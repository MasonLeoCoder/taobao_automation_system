"""发布类目事务：完整列才可核销缺失项，采集与方案配置分开。"""
from contextlib import contextmanager
from collections import Counter
import json

import sqlalchemy as sa

from ecommerce.infrastructure.storage.collection_store import collection_store
from ecommerce.infrastructure.storage.environments import assert_writable
from ecommerce.infrastructure.storage.mysql import MySQLStorageError, safe_error
from ecommerce.infrastructure.storage.time_values import database_now

SEPARATOR = ' > '


@contextmanager
def connection(settings, *, write=False):
    if write:
        assert_writable(settings.mysql)
    store = collection_store(settings)
    try:
        with (store.engine.begin() if write else store.engine.connect()) as conn:
            yield conn, store.tables['publish_categories'], store.tables['publish_category_scans']
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None


def catalog(settings):
    with connection(settings) as (conn, categories, scans):
        rows = conn.execute(sa.select(categories).order_by(categories.c.full_path)).mappings()
        history = conn.execute(sa.select(scans).order_by(scans.c.started_at.desc()).limit(12)).mappings()
        return [dict(row) for row in rows], [dict(row) for row in history]


def find(settings, path):
    with connection(settings) as (conn, table, _):
        return conn.execute(sa.select(table).where(table.c.full_path == path)).mappings().one_or_none()


def has_complete_branch(settings, path):
    with connection(settings) as (conn, _, scans):
        return bool(conn.scalar(sa.select(scans.c.id).where(scans.c.status == 'complete',
            sa.func.json_contains(scans.c.result, json.dumps(path, ensure_ascii=False), '$.completed')).limit(1)))


def begin_scan(settings, identifier, scopes):
    with connection(settings, write=True) as (conn, _, scans):
        # 浏览器任务已持有会话锁，前序运行记录只能来自已退出的采集。
        conn.execute(scans.update().where(scans.c.status == 'running').values(
            status='failed', finished_at=database_now(),
            result={'failed': [{'path': '', 'reason': '前次采集已中断，请按原范围重试'}]}))
        conn.execute(scans.insert().values(id=identifier, scopes=scopes, status='running',
            result={}, started_at=database_now()))


def fail_scan(settings, identifier, reason):
    with connection(settings, write=True) as (conn, _, scans):
        conn.execute(scans.update().where(scans.c.id == identifier).values(
            status='failed', result={'failed': [{'path': '', 'reason': reason}]},
            finished_at=database_now()))


def apply_scan(settings, identifier, observed, complete, failed, initial_profiles):
    """合并整批已确认的类目与分支结果，并原子保存本次审计。"""
    with connection(settings, write=True) as (conn, table, scans):
        old = [dict(row) for row in conn.execute(sa.select(table).with_for_update()).mappings()]
        by_path = {row['full_path']: row for row in old}
        by_id = {row['qianniu_category_id']: row for row in old if row['qianniu_category_id']}
        id_counts = Counter(platform_id for platform_id, _ in observed.values() if platform_id)
        result = dict(added=[], changed=[], hidden=[], conflicts=[], completed=[], failed=failed)
        seen = set()
        leaves = set()
        blocked = set()
        original = {row['id']: dict(row) for row in old}
        now = database_now()
        for parts, entry in sorted(observed.items(), key=lambda item: (len(item[0]), item[0])):
            path, parent_path = SEPARATOR.join(parts), SEPARATOR.join(parts[:-1])
            platform_id, leaf = entry
            parent = by_path.get(parent_path)
            at_path, at_id = by_path.get(path), by_id.get(platform_id)
            if (parts[:-1] in blocked or (parent_path and not parent) or id_counts[platform_id] > 1
                    or (at_path and platform_id and at_path['qianniu_category_id'] not in (None, platform_id))
                    or (at_path and at_id and at_path['id'] != at_id['id'])
                    or (at_id and at_id['id'] in seen)):
                blocked.add(parts)
                result['conflicts'].append({'path': path, 'previous': (at_id or at_path or {}).get('full_path'),
                                            'platform_id': platform_id})
                affected_paths = [item['full_path'] for item in (at_path, at_id) if item]
                affected = [item['id'] for item in old if item['listing_profile_key'] and any(
                    item['full_path'] == previous or item['full_path'].startswith(previous + SEPARATOR)
                    for previous in affected_paths)]
                if affected:
                    conn.execute(table.update().where(table.c.id.in_(affected)).values(profile_status='review'))
                continue
            row = at_id or at_path
            data = dict(parent_id=parent['id'] if parent else None, name=parts[-1], full_path=path,
                        level=len(parts), is_leaf=leaf, is_visible=True, last_seen_at=now)
            if platform_id:
                data['qianniu_category_id'] = platform_id
            if row:
                old_path = row['full_path']
                changes = {key: [row[key], data[key]] for key in ('full_path', 'is_leaf', 'is_visible', 'parent_id')
                           if row[key] != data[key]}
                if changes:
                    result['changed'].append({'before': old_path, 'after': path, 'fields': changes})
                    if row['listing_profile_key']:
                        data['profile_status'] = 'review'
                conn.execute(table.update().where(table.c.id == row['id']).values(**data))
                if old_path != path:
                    affected = [item['id'] for item in original.values()
                                if item['listing_profile_key'] and item['full_path'].startswith(old_path + SEPARATOR)]
                    if affected:
                        conn.execute(table.update().where(table.c.id.in_(affected)).values(profile_status='review'))
                    del by_path[old_path]
                row.update(data)
            else:
                # 已有不可见同名末级可能是改名或重建。仅首见时接入既有方案。
                key = initial_profiles.get(path) if leaf else None
                if key and any(previous['listing_profile_key'] == key for previous in original.values()):
                    key = None
                data.update(first_seen_at=now, listing_profile_key=key, profile_status='verified' if key else None)
                inserted = conn.execute(table.insert().values(**data))
                row = dict(data, id=inserted.inserted_primary_key[0])
                result['added'].append(path)
            by_path[path] = row
            if platform_id:
                by_id[platform_id] = row
            seen.add(row['id'])
            if leaf:
                leaves.add(row['id'])

        complete_ids = leaves
        for parts in complete:
            path = SEPARATOR.join(parts)
            if parts not in blocked and not any(conflict[:-1] == parts for conflict in blocked):
                if not parts or path in by_path:
                    complete_ids.add(by_path[path]['id'] if parts else None)
                    result['completed'].append(path)

        # 按旧父子关系处理缺失分支；迁移后已确认可见的条目优先。
        missing = set()
        for row in sorted(original.values(), key=lambda item: item['level']):
            if row['id'] not in seen and (row['parent_id'] in complete_ids or row['parent_id'] in missing):
                missing.add(row['id'])
                if row['is_visible']:
                    changes = {'is_visible': False}
                    if row['listing_profile_key']:
                        changes['profile_status'] = 'review'
                    conn.execute(table.update().where(table.c.id == row['id']).values(**changes))
                    result['hidden'].append(row['full_path'])
        # 缺少平台 ID 的同批消失及新增保留差异，供人工核对。
        if result['hidden'] and result['added']:
            for path in result['added']:
                if not by_path[path].get('qianniu_category_id'):
                    result['conflicts'].append({'path': path, 'previous': result['hidden'],
                                                'platform_id': None})
        status = 'partial' if (failed or result['conflicts']) and observed else 'failed' if failed else 'complete'
        conn.execute(scans.update().where(scans.c.id == identifier).values(
            result=result, status=status, finished_at=now))
        return status
