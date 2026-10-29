"""单账号文件凭据、后台引用和异常脱敏的离线验证。"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ecommerce.shared.config import (load_settings,load_mysql_settings,mysql_settings_payload,
    mysql_settings_from_payload,settings_payload)
from ecommerce.infrastructure.storage.mysql import create_mysql_engine,MySQLStorageError
from ecommerce.infrastructure.storage.mysql_verification import database_read_write_granted
from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
from ecommerce.infrastructure.tasks import web_jobs
from ecommerce.modules.collection.web_worker import settings_from_payload


class MySQLCredentialTests(unittest.TestCase):
    def setUp(self):
        """创建隔离配置文件；参数：无；返回：无，不连接实际MySQL。"""
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.path=self.root/'config/settings.toml'
        self.path.parent.mkdir()
        self.secret='synthetic-password-for-tests-only'

    def configure(self,password=None,*,omit=False,user='root'):
        """保存脱敏配置并加载；参数：密码、是否省略、账号；返回：配置。"""
        text='[mysql]\nuser='+json.dumps(user)+'\n'
        if not omit:
            text+='password='+json.dumps(self.secret if password is None else password)+'\n'
        self.path.write_text(text,encoding='utf-8')
        return load_settings(root=self.root)

    def test_file_password_wins_and_runtime_upgrade_share_one_account(self):
        """文件密码优先于旧环境变量，运行与升级使用相同账号和密码。"""
        settings=self.configure()
        with patch.dict('os.environ',{'ECOMMERCE_MYSQL_PASSWORD':'obsolete-environment-password'}):
            engines=[create_mysql_engine(settings.mysql,migration=value) for value in (False,True)]
        for engine in engines:
            self.addCleanup(engine.dispose)
            self.assertEqual((engine.url.username,engine.url.password),('root',self.secret))
            self.assertNotIn(self.secret,str(engine.url))
        self.assertIsNone(settings.mysql.password)
        self.assertNotIn(self.secret,repr(settings))
        self.assertEqual(settings.mysql.credentials_file,str(self.path))

    def test_empty_explicit_password_does_not_fall_back_to_old_environment(self):
        """管理员密码空白时明确阻止连接，不误用原程序账号的环境密码。"""
        settings=self.configure('')
        with patch.dict('os.environ',{'ECOMMERCE_MYSQL_PASSWORD':'obsolete-environment-password'}),self.assertRaisesRegex(MySQLStorageError,'mysql.*password'):
            create_mysql_engine(settings.mysql)

    def test_environment_fallback_and_old_migration_fields_do_not_choose_second_account(self):
        """未填写文件密码时兼容单一环境变量，旧迁移账号不能覆盖当前账号。"""
        settings=self.configure(omit=True)
        legacy=load_mysql_settings({'user':'root','migration_user':'obsolete_user','migration_password_env':'OBSOLETE_PASSWORD'})
        self.assertFalse(hasattr(legacy,'migration_user'))
        with patch.dict('os.environ',{'ECOMMERCE_MYSQL_PASSWORD':self.secret,'OBSOLETE_PASSWORD':'wrong-password'}):
            for config in (settings.mysql,legacy):
                engine=create_mysql_engine(config,migration=True)
                self.addCleanup(engine.dispose)
                self.assertEqual((engine.url.username,engine.url.password),('root',self.secret))

    def test_task_and_child_process_snapshot_contain_only_credential_reference(self):
        """任务快照和后台引导均仅含文件引用，不复制明文密码。"""
        settings=self.configure()
        payload=settings_payload(settings)
        self.assertNotIn(self.secret,json.dumps(payload))
        self.assertNotIn('password',payload['mysql'])
        self.assertEqual(payload['mysql']['credentials_file'],str(self.path))
        restored=settings_from_payload(payload)
        engine=create_mysql_engine(restored.mysql)
        self.addCleanup(engine.dispose)
        self.assertEqual(engine.url.password,self.secret)
        with patch.object(web_jobs.subprocess,'Popen') as launch:
            web_jobs.launch(settings,'a'*32,'b'*32)
        bootstrap=json.loads(launch.call_args.kwargs['env']['ECOMMERCE_JOB_BOOTSTRAP'])
        self.assertNotIn(self.secret,json.dumps(bootstrap))
        self.assertNotIn('password',bootstrap['mysql'])

    def test_job_can_use_current_admin_credentials_without_changing_database_target(self):
        """旧账号快照可绑定当前管理员，另一个数据库目标仍拒绝。"""
        settings=self.configure()
        payload=settings_payload(settings)
        payload['mysql'].update(user='taobao_app',migration_user='taobao_migrator',migration_password_env='OLD_PASSWORD')
        payload['mysql'].pop('credentials_file')
        restored=settings_from_payload(payload,mysql=settings.mysql)
        self.assertEqual(restored.mysql.user,'root')
        engine=create_mysql_engine(restored.mysql)
        self.addCleanup(engine.dispose)
        self.assertEqual(engine.url.password,self.secret)
        payload['mysql']['database']='another_database'
        with self.assertRaisesRegex(ValueError,'目标不匹配'):
            settings_from_payload(payload,mysql=settings.mysql)

    def test_plaintext_snapshot_is_rejected_before_database_write(self):
        """存储入口拒绝意外携带密码的任务，不打开事务。"""
        config={'user':'root','password':self.secret}
        with self.assertRaises(ValueError) as error:
            mysql_settings_from_payload(config)
        self.assertNotIn(self.secret,str(error.exception))
        store=object.__new__(CollectionJobStore)
        with self.assertRaises(ValueError):
            store.submit({'config':{'mysql':config}})
        with self.assertRaisesRegex(ValueError,'本地配置文件'):
            mysql_settings_payload(load_mysql_settings(config))

    def test_invalid_local_config_and_account_changes_do_not_expose_password(self):
        """无效配置和账号变化只报告原因，不打印文件内容。"""
        settings=self.configure()
        self.path.write_text('[mysql]\npassword="'+self.secret+'"\ninvalid @',encoding='utf-8')
        for action in (lambda:create_mysql_engine(settings.mysql),lambda:load_settings(root=self.root)):
            with self.assertRaises((ValueError,MySQLStorageError)) as error:
                action()
            self.assertNotIn(self.secret,str(error.exception))
        self.configure(user='different_user')
        with self.assertRaisesRegex(MySQLStorageError,'重新加载'):
            create_mysql_engine(settings.mysql)

    def test_privilege_check_accepts_global_admin_and_checks_database_scope(self):
        """全局管理员和目标库读写均通过，其他库或缺失权限不误报成功。"""
        self.assertTrue(database_read_write_granted(['GRANT ALL PRIVILEGES ON *.* TO test'],'sample_verify'))
        self.assertTrue(database_read_write_granted(['GRANT SELECT, INSERT, UPDATE, DELETE ON `sample_verify`.* TO test'],'sample_verify'))
        self.assertFalse(database_read_write_granted(['GRANT ALL PRIVILEGES ON `other`.* TO test'],'sample_verify'))
        self.assertFalse(database_read_write_granted(['GRANT SELECT ON *.* TO test'],'sample_verify'))
