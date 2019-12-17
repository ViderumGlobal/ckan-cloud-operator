# VARIABLES
variable "region" {
  default  = "eu-west-2"
}

variable "vpc_id" {
  default = "vpc-30aedd58"
}

variable "cluster_name" {
   default = "terraform-cco"
}

# AWS GENERIC
provider "aws" {
  region  = var.region
}

# VPC DATA
data "aws_vpc" "selected" {
  id = var.vpc_id
}

data "aws_subnet_ids" "selected" {
  vpc_id = var.vpc_id
}

# K8S CLUSTER
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "7.0.1"

  cluster_name    = var.cluster_name
  cluster_version = "1.14"

  subnets = data.aws_subnet_ids.selected.ids
  vpc_id = var.vpc_id

  write_kubeconfig = true
}

resource "aws_security_group_rule" "allow_inner_cluster" {
  security_group_id = module.eks.cluster_security_group_id
  source_security_group_id = module.eks.cluster_security_group_id
  type = "ingress"
  from_port = 0
  to_port = 65535
  protocol = "tcp"
}

# resource "aws_iam_role" "cco-cluster" {
#   name = "eks-cluster"

#   assume_role_policy = <<POLICY
# {
#   "Version": "2012-10-17",
#   "Statement": [
#     {
#       "Effect": "Allow",
#       "Principal": {
#         "Service": "eks.amazonaws.com"
#       },
#       "Action": "sts:AssumeRole"
#     }
#   ]
# }
# POLICY
# }

# resource "aws_iam_role_policy_attachment" "cco-AmazonEKSClusterPolicy" {
#   policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
#   role       = aws_iam_role.cco-cluster.name
# }

# resource "aws_iam_role_policy_attachment" "cco-AmazonEKSServicePolicy" {
#   policy_arn = "arn:aws:iam::aws:policy/AmazonEKSServicePolicy"
#   role       = aws_iam_role.cco-cluster.name
# }

# K8S NODE GROUP
resource "aws_iam_role" "cco-nodegroup" {
  name = "eks-node-group"

  assume_role_policy = jsonencode({
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
    }]
    Version = "2012-10-17"
  })
}

resource "aws_iam_role_policy_attachment" "cco-AmazonEKSWorkerNodePolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
  role       = aws_iam_role.cco-nodegroup.name
}

resource "aws_iam_role_policy_attachment" "cco-AmazonEKS_CNI_Policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
  role       = aws_iam_role.cco-nodegroup.name
}

resource "aws_iam_role_policy_attachment" "cco-AmazonEC2ContainerRegistryReadOnly" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
  role       = aws_iam_role.cco-nodegroup.name
}

resource "aws_eks_node_group" "cco-nodegroup" {
  cluster_name    = module.eks.cluster_id
  node_group_name = "${module.eks.cluster_id}-nodegroup"
  node_role_arn   = aws_iam_role.cco-nodegroup.arn
  subnet_ids      = data.aws_subnet_ids.selected.ids

  scaling_config {
    desired_size = 3
    max_size     = 5
    min_size     = 3
  }

  # Ensure that IAM Role permissions are created before and deleted after EKS Node Group handling.
  # Otherwise, EKS will not be able to properly delete EC2 Instances and Elastic Network Interfaces.
  depends_on = [
    aws_iam_role_policy_attachment.cco-AmazonEKSWorkerNodePolicy,
    aws_iam_role_policy_attachment.cco-AmazonEKS_CNI_Policy,
    aws_iam_role_policy_attachment.cco-AmazonEC2ContainerRegistryReadOnly,
  ]
}

# K8S MASTER
data "aws_eks_cluster" "cluster" {
  name = module.eks.cluster_id  
}

data "aws_eks_cluster_auth" "cluster" {
  name = module.eks.cluster_id
}

provider "kubernetes" {
  host                   = data.aws_eks_cluster.cluster.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.cluster.certificate_authority.0.data)
  token                  = data.aws_eks_cluster_auth.cluster.token
  load_config_file       = false
  version                = "~> 1.9"
}

# RDS
resource "random_password" "rds_password" {
  length = 16
  special = false
}

resource "aws_db_instance" "default" {
  allocated_storage    = 20
  max_allocated_storage = 100
  storage_type         = "gp2"
  engine               = "postgres"
  engine_version       = "11.5"
  instance_class       = "db.m4.large"
  name                 = replace("${var.cluster_name}-db", "-", "_")
  username             = "ckan"
  password             = random_password.rds_password.result
  vpc_security_group_ids = [aws_security_group.allow_postgres.id]
  final_snapshot_identifier  = "some-snap"
  skip_final_snapshot = true
}

resource "aws_security_group" "allow_postgres" {
  name        = "allow_postgres"
  description = "Allow Postgres inbound traffic"
  vpc_id      = var.vpc_id

  ingress {    
    from_port = 5432
    to_port = 5432
    protocol = "tcp"
    security_groups = [module.eks.cluster_security_group_id]
  }
}

# NFS
resource "aws_efs_file_system" "default" {
}

resource "aws_efs_mount_target" "default" {
  for_each = toset(data.aws_subnet_ids.selected.ids)

  file_system_id = aws_efs_file_system.default.id
  subnet_id = each.key
  security_groups = [aws_security_group.allow_nfs.id]
}

resource "aws_security_group" "allow_nfs" {
  name        = "allow_nfs"
  description = "Allow NFS inbound traffic"
  vpc_id      = var.vpc_id

  ingress {    
    from_port = 2049
    to_port = 2049
    protocol = "tcp"
    security_groups = [module.eks.cluster_security_group_id]
  }
}

# OUTPUT

output "cco-interactive-yaml" {
  value = <<YAML
default:
  config:
    routers-config:
      env-id: p
      default-root-domain: localhost
      dns-provider: none
    aws-efs:
      file.system.id: ${aws_efs_file_system.default.id}
  secrets:
    ckan-cloud-provider-cluster-aws:
      aws-access-key-id: x
      aws-secret-access-key: x
      aws-default-region: ${var.region}
      eks-cluster-name: ${module.eks.cluster_id}

    solr-config:
      self-hosted: y
      num-shards: "1"
      replication-factor: "3"
    ckan-storage-config:
      default-storage-bucket: ckan
    rds:
      rds-instance-name: ${aws_db_instance.default.id}
      rds-host: ${aws_db_instance.default.address}
      admin-user: ckan
      admin-password: "${random_password.rds_password.result}"
YAML
}