"""迁移测试专用空验证库前态；禁止对非空库或正式库重建。"""
import sqlalchemy as sa


def reset_empty_validation(connection, metadata, revision):
    """重建空验证库的指定版本；参数：连接、冻结模型、版本；返回：无。"""
    if not connection.engine.url.database.endswith('_verify'):
        raise AssertionError('迁移前态演练禁止使用正式库')
    actual = sa.MetaData()
    actual.reflect(connection)
    for name, table in actual.tables.items():
        if name != 'alembic_version' and connection.scalar(sa.select(sa.func.count()).select_from(table)):
            raise AssertionError('验证库非空，不重建迁移前态：' + name)
    connection.commit()
    if 'alembic_version' in actual.tables:
        actual.remove(actual.tables['alembic_version'])
    actual.drop_all(connection)
    if metadata is not None:
        metadata.create_all(connection)
    if revision is None:
        if sa.inspect(connection).has_table('alembic_version'):
            connection.exec_driver_sql('DROP TABLE alembic_version')
    else:
        connection.execute(sa.text('UPDATE alembic_version SET version_num=:revision'), {'revision': revision})
    connection.commit()
