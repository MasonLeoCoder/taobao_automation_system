"""MySQL 基础配置、凭据边界和结构契约的脱敏回归检查。"""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from ecommerce.shared.config import load_settings,load_mysql_settings
from ecommerce.infrastructure.storage.mysql import MySQLStorageError,create_mysql_engine,safe_error,_normalize_type,version_table
from ecommerce.infrastructure.storage.mysql_dictionary import render_dictionary
from ecommerce.infrastructure.storage.mysql_checks import check_expression_tree
from ecommerce.infrastructure.storage.mysql_migrations.schema_v5 import build_metadata
from ecommerce.infrastructure.storage.mysql_verification import verify_foundation


class MySQLFoundationTests(unittest.TestCase):
    """只使用临时配置和假凭据，不连接真实店铺数据库。"""

    def test_mysql_config_is_separate_from_legacy_storage(self):
        """新增本机配置不改变现有 SQLite 和浏览器路径；参数：无；返回：无。"""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'config').mkdir()
            (root/'config/settings.toml').write_text('[mysql]\ndatabase="sample_verify"\nport=3307\n',encoding='utf-8')
            settings=load_settings(root=root)
            self.assertEqual(settings.mysql.database,'sample_verify')
            self.assertEqual(settings.mysql.port,3307)
            self.assertEqual(settings.database,root/'workspace/storage/database/ecommerce.sqlite3')

    def test_bad_and_plaintext_secret_config_rejected(self):
        """拒绝远程、非法标识、布尔端口和明文密码配置；参数：无；返回：无。"""
        for value in ({'host':'example.com'},{'port':True},{'database':'db;DROP DATABASE other'},
                      {'user':'bad-user'},{'password':'must_not_be_stored'},{'connect_timeout':0},{'password_env':'bad env'},
                      {'user':'root'},{'migration_user':'taobao_app'},{'user':'a'*33}):
            with self.subTest(keys=list(value)),self.assertRaises(ValueError):
                load_mysql_settings(value)

    def test_secret_is_not_in_configuration_or_error_output(self):
        """凭据不进入配置对象、URL 显示或数据库错误摘要；参数：无；返回：无。"""
        config=load_mysql_settings({})
        secret='synthetic-secret-should-not-appear'
        with patch.dict(os.environ,{config.password_env:secret}):
            engine=create_mysql_engine(config)
            self.addCleanup(engine.dispose)
            self.assertNotIn(secret,str(engine.url))
            self.assertNotIn(secret,repr(config))
        error=OperationalError('sql '+secret,{'password':secret},Exception(1045,secret))
        self.assertNotIn(secret,safe_error(error))
        self.assertIn('1045',safe_error(error))

    def test_missing_password_fails_without_connecting(self):
        """没有凭据时给出环境变量说明，不尝试空密码连接；参数：无；返回：无。"""
        config=load_mysql_settings({})
        with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(MySQLStorageError,config.password_env):
            create_mysql_engine(config)

    def test_all_tables_and_columns_have_comments_and_dictionary_entries(self):
        """备注及完整字段字典覆盖所有业务表与技术版本表；参数：无；返回：无。"""
        metadata=build_metadata()
        self.assertEqual(len(metadata.tables),18)
        dictionary=render_dictionary()
        self.assertEqual(dictionary,render_dictionary(),"同版字段字典应稳定生成，不能因外键集合顺序变化而产生差异")
        for table in list(metadata.tables.values())+[version_table()]:
            self.assertTrue(table.comment)
            self.assertIn('## '+table.name,dictionary)
            for column in table.columns:
                self.assertTrue(column.comment,(table.name,column.name))
                self.assertIn('`'+column.name+'`',dictionary)
                if isinstance(column.type,sa.JSON):
                    self.assertTrue(column.info.get('json_contract'))

    def test_ownership_is_encoded_in_composite_foreign_keys(self):
        """关键归属不能只校验单个编号存在；参数：无；返回：无。"""
        metadata=build_metadata()
        expected={'collection_batches':('step_id','run_id','module'),
                  'product_current_sections':('batch_id','product_id','module','scope_key'),
                  'answer_observations':('question_id','batch_id')}
        for name,columns in expected.items():
            self.assertTrue(any(tuple(e.parent.name for e in fk.elements)==columns for fk in metadata.tables[name].foreign_key_constraints))
        self.assertTrue(all(fk.ondelete=='RESTRICT' and fk.onupdate=='RESTRICT' for t in metadata.tables.values() for fk in t.foreign_key_constraints))

    def test_mysql_reflection_character_set_spelling(self):
        """字符集反射的附加拼写不造成字段类型误报；参数：无；返回：无。"""
        self.assertEqual(_normalize_type('VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin'),_normalize_type('VARCHAR(32) COLLATE ascii_bin'))
        self.assertEqual(_normalize_type('BOOL'),_normalize_type('TINYINT(1)'))
        self.assertNotEqual(_normalize_type('DECIMAL(20,6)'),_normalize_type('DECIMAL(20,2)'))

    def test_synthetic_validation_refuses_non_validation_database(self):
        """合成验证不允许误写正式库；参数：无；返回：无。"""
        with self.assertRaisesRegex(MySQLStorageError,'_verify'):
            verify_foundation(replace(load_mysql_settings({}),database='taobao_automation'))

    def test_check_reflection_preserves_semantics(self):
        """MySQL 括号及字符集前缀可兼容，优先级或边界变更必须识别；参数：无；返回：无。"""
        self.assertEqual(check_expression_tree("state IN ('a','b')"),check_expression_tree("(`state` in (_ascii'a',_ascii'b'))"))
        self.assertEqual(check_expression_tree('(a IS NOT NULL)+(b IS NOT NULL)+(c IS NOT NULL)<=1'),
                         check_expression_tree('((((a is not null)+(b is not null))+(c is not null))<=1)'))
        self.assertNotEqual(check_expression_tree('a=1 OR b=2 AND c=3'),check_expression_tree('(a=1 OR b=2) AND c=3'))
        self.assertNotEqual(check_expression_tree('x BETWEEN 0 AND 100'),check_expression_tree('x BETWEEN 0 AND 101'))
        with self.assertRaises(ValueError):
            check_expression_tree('unsupported_function(x)')
