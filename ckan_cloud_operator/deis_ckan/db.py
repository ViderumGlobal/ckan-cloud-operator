import subprocess
import binascii
import os
import yaml
import time
import traceback

from ckan_cloud_operator.datastore_permissions import DATASTORE_PERMISSIONS_SQL_TEMPLATE
from ckan_cloud_operator import gcloud
from ckan_cloud_operator.db import manager as db_manager


class DeisCkanInstanceDb(object):

    def __init__(self, instance, db_type):
        self.instance = instance
        assert db_type in ['db', 'datastore']
        self.db_type = db_type
        self.db_spec = getattr(self.instance.spec, db_type)

    def update(self):
        is_created = self.instance.annotations.update_status(self.db_type, 'created', lambda: self._create())
        skip_permissions_flag = 'skipDatastorePermissions' if self.db_type == 'datastore' else 'skipDbPermissions'
        if is_created or not self.instance.annotations.get_flag(skip_permissions_flag):
            db_manager.update(deis_instance_id=self.instance.id)
            self._set_db_permissions()
            if self.db_type == 'datastore':
                self._set_datastore_permissions()
        if is_created:
            self.instance.annotations.set_flag(skip_permissions_flag)

    def delete(self):
        db_name = self.db_spec['name']
        self._psql(f'DROP DATABASE IF EXISTS "{db_name}";')
        # TODO: get the ro_user from the instance annotations
        self._psql(f'DROP ROLE IF EXISTS "{db_name}";')
        self._psql(f'DROP ROLE IF EXISTS "{db_name}-ro";')

    def get(self):
        db_name = self.db_spec['name']
        try:
            self._psql('select 1', '-d', db_name, '-q', '-o', '/dev/null')
            return {'ready': True}
        except Exception:
            traceback.print_exc()
            return {'ready': False}
        # gcloud sql commands related to DBs don't work due to missing permissions to gcloud account
        # TODO: allow to run gcloud db commands
        # exitcode, output = gcloud.getstatusoutput(
        #     f'-q sql databases describe {db_name} --instance {gcloud_sql_instance_name}',
        #     ckan_infra=self.instance.ckan_infra
        # )
        # if exitcode == 0:
        #     gcloud_status = yaml.load(output)
        #     assert gcloud_status['instance'] == gcloud_sql_instance_name
        #     assert gcloud_status['name'] == db_name
        #     assert gcloud_status['project'] == gcloud_sql_project
        #     return {'ready': True,
        #             'name': db_name,
        #             'selfLink': gcloud_status['selfLink']}
        # else:
        #     return {'ready': False,
        #             'name': db_name,
        #             'gcloud_sql_instance_name': gcloud_sql_instance_name,
        #             'gcloud_sql_project': gcloud_sql_project,
        #             'error': output}

    def _create(self):
        print(f'Creating {self.db_type}')
        if 'fromDeisInstance' in self.db_spec:
            raise NotImplementedError('import of DB from old deis instance id is not supported yet')
        else:
            self._create_base_db()
            db_manager.update(deis_instance_id=self.instance.id)
            db_name = self.db_spec['name']
            if self.db_type == 'db':
                self._initialize_db_postgis(db_name)
            if 'importGcloudSqlDumpUrl' in self.db_spec:
                self._import_gcloud_sql_db()
            if self.db_type == 'datastore':
                self._create_datastore_ro_user()

    def _psql(self, cmd, *args, db_user=None, db_password=None):
        if os.environ.get('CKAN_CLOUD_OPERATOR_USE_PROXY') in ['yes', '1', 'true']:
            postgres_host = '127.0.0.1'
        else:
            postgres_host = self.instance.ckan_infra.POSTGRES_HOST
        if not db_user:
            db_user = self.instance.ckan_infra.POSTGRES_USER
        if not db_password:
            db_password = self.instance.ckan_infra.POSTGRES_PASSWORD
        subprocess.check_call(['psql', '-v', 'ON_ERROR_STOP=on', '-h', postgres_host,
                               '-U', db_user, *args, '-c', cmd],
                              env={'PGPASSWORD': db_password})

    def _set_db_permissions(self):
        print('setting db permissions')
        db_name = self.db_spec['name']
        postgres_user = self.instance.ckan_infra.POSTGRES_USER
        for line in [
            f"GRANT \"{db_name}\" TO \"{postgres_user}\";",
            f'GRANT ALL PRIVILEGES ON DATABASE "{db_name}" to "{db_name}";',
            f'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public to "{db_name}";',
            f'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public to "{db_name}";',
            f'GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public to "{db_name}";',
            f'alter default privileges in schema public grant all on tables to "{db_name}";',
            f'alter default privileges in schema public grant all on sequences to "{db_name}";',
            f'alter default privileges in schema public grant all on functions to "{db_name}";',
            f"ALTER DATABASE \"{db_name}\" OWNER TO \"{db_name}\";",
        ]:
            self._psql(line, '-d', db_name)

    def _set_datastore_permissions(self):
        print('setting datastore permissions')
        db_name = self.db_spec['name']
        ro_user = f'{db_name}-ro'
        site_user = self.instance.spec.db['name']
        postgres_user = self.instance.ckan_infra.POSTGRES_USER
        for line in [
            f"REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
            f"REVOKE USAGE ON SCHEMA public FROM PUBLIC;",
            f"GRANT CREATE ON SCHEMA public TO \"{site_user}\";",
            f"GRANT USAGE ON SCHEMA public TO \"{site_user}\";",
            f"GRANT CREATE ON SCHEMA public TO \"{db_name}\";",
            f"GRANT USAGE ON SCHEMA public TO \"{db_name}\";",
            f"GRANT \"{site_user}\" TO \"{postgres_user}\";",
            f"ALTER DATABASE \"{site_user}\" OWNER TO {postgres_user};",
            f"ALTER DATABASE \"{db_name}\" OWNER TO {postgres_user};",
            f"REVOKE CONNECT ON DATABASE \"{site_user}\" FROM \"{ro_user}\";",
            f"GRANT CONNECT ON DATABASE \"{db_name}\" TO \"{ro_user}\";",
            f"GRANT USAGE ON SCHEMA public TO \"{ro_user}\";",
            f"ALTER DATABASE \"{site_user}\" OWNER TO \"{site_user}\";",
            f"GRANT \"{db_name}\" TO \"{postgres_user}\";",
            f"ALTER DATABASE \"{db_name}\" OWNER TO \"{db_name}\";",
            f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO \"{ro_user}\";",
            f"ALTER DEFAULT PRIVILEGES FOR USER \"{db_name}\" IN SCHEMA public GRANT SELECT ON TABLES TO \"{ro_user}\";",
            f"GRANT \"{site_user}\" to \"{db_name}\";",
        ]:
            self._psql(line, '-d', db_name)
        datastore_permissions = DATASTORE_PERMISSIONS_SQL_TEMPLATE.replace('{{SITE_USER}}', site_user).replace('{{DS_RO_USER}}', ro_user)
        self._psql(datastore_permissions, "-d", db_name)

    def _create_datastore_ro_user(self):
        db_name = self.db_spec['name']
        ro_user = f'{db_name}-ro'
        ro_password = binascii.hexlify(os.urandom(12)).decode()
        self.instance.annotations.set_secrets({'datastoreReadonlyUser': ro_user, 'datatastoreReadonlyPassword': ro_password})
        self._psql(
            f"CREATE ROLE \"{ro_user}\" WITH LOGIN PASSWORD '{ro_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE;"
        )

    def _set_gcloud_storage_sql_permissions(self, importUrl):
        print('setting permissions to cloud storage for import to sql')
        gcloud_sql_instance_name = self.instance.ckan_infra.GCLOUD_SQL_INSTANCE_NAME
        gcloud_sql_instance = yaml.load(gcloud.check_output(
            f'sql instances describe {gcloud_sql_instance_name}',
            ckan_infra=self.instance.ckan_infra
        ))
        gcloud_sql_service_account_email = gcloud_sql_instance['serviceAccountEmailAddress']
        gcloud.check_call(
            f'acl ch -u {gcloud_sql_service_account_email}:R {importUrl}',
            gsutil=True,
            ckan_infra=self.instance.ckan_infra
        )

    def _create_base_db(self):
        password_secret_name = 'datastorePassword' if self.db_type == 'datastore' else 'databasePassword'
        if self.instance.annotations.get_secret(password_secret_name):
            print('Password already exists, skipping DB creation')
            return False
        else:
            print('Creating base db')
            db_name = self.db_spec['name']
            db_password = binascii.hexlify(os.urandom(12)).decode()
            self.instance.annotations.set_secret(password_secret_name, db_password)
            self._psql(
                f'CREATE ROLE "{db_name}" WITH LOGIN PASSWORD \'{db_password}\' NOSUPERUSER NOCREATEDB NOCREATEROLE;')
            self._psql(f'CREATE DATABASE "{db_name}";')

    def _initialize_db_postgis(self, db_name):
        print('initializing postgis extensions')
        self._psql('CREATE EXTENSION IF NOT EXISTS postgis;', '-d', db_name)
        self._psql('CREATE EXTENSION IF NOT EXISTS postgis_topology;', '-d', db_name)
        self._psql('CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;', '-d', db_name)
        self._psql('CREATE EXTENSION IF NOT EXISTS postgis_tiger_geocoder;', '-d', db_name)

    def _import_gcloud_sql_db(self):
        gcloud_sql_instance_name = self.instance.ckan_infra.GCLOUD_SQL_INSTANCE_NAME
        db_name = self.db_spec['name']
        importUrl = self.db_spec["importGcloudSqlDumpUrl"]
        print(f'Importing Gcloud SQL from: {importUrl}')
        self._set_gcloud_storage_sql_permissions(importUrl)
        postgres_user = self.instance.ckan_infra.POSTGRES_USER
        returncode, output = gcloud.getstatusoutput(
            f'--quiet sql import sql {gcloud_sql_instance_name} {importUrl} --database={db_name} --user={postgres_user} '
            f'  | tee /dev/stderr',
            ckan_infra=self.instance.ckan_infra
        )
        if returncode == 1:
            if 'You can continue waiting for the operation by running `gcloud beta sql operations wait --project ckan-cloud ' in output:
                operation_id = output.split('You can continue waiting for the operation by running `gcloud beta sql operations wait --project ckan-cloud ')[1].split('`')[0]
                print(f'Waiting for sql import operation {operation_id} to complete...')
                while time.sleep(5):
                    operation = yaml.load(gcloud.check_output(f'sql operations describe {operation_id}'))
                    print(operation['status'])
                    if operation['status'] == 'DONE':
                        break
