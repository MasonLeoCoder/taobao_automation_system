"""任务存储兼容入口：保留旧 SQLite 的显式调用，正式任务按配置使用 MySQL。"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from ecommerce.infrastructure.storage.time_values import BEIJING
from pathlib import Path
from uuid import uuid4


def now() -> str:
    """生成统一时间；参数：无；返回：北京时间ISO字符串。"""
    return datetime.now(BEIJING).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path):
    """打开并关闭数据库；参数：文件路径；返回：连接上下文。"""
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize(path: Path) -> None:
    """事务式执行新增迁移；参数：数据库路径；返回：无。"""
    if not isinstance(path,Path):
        return path.initialize()
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        migrations = sorted((Path(__file__).parent / "migrations").glob("*.sql"))
        latest = max(int(item.name.split("_")[0]) for item in migrations)
        if version > latest:
            raise RuntimeError("数据库版本高于当前程序，请使用更新版本的程序")
        for migration in migrations:
            number = int(migration.name.split("_")[0])
            if number <= version:
                continue
            statement = ""
            for line in migration.read_text(encoding="utf-8").splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise ValueError(f"迁移 SQL 不完整：{migration.name}")
            connection.execute(f"PRAGMA user_version = {number}")


def create_task(path: Path, name: str) -> str:
    """创建运行记录；参数：数据库路径、任务名；返回：任务 ID。"""
    task_id = uuid4().hex
    if not isinstance(path,Path):
        operation={'op':'create','database':path.settings.database,'task_id':task_id,'event_key':uuid4().hex,'time':now(),
                   'event_type':'task_started','state':'running','message':'任务开始：'+name,
                   'task':{'id':task_id,'name':name,'status':'running','started_at':now(),'origin':'live'}}
        path.apply_operation(operation)
        return task_id
    with connect(path) as connection:
        connection.execute("INSERT INTO tasks(id,name,status,started_at) VALUES (?,?,'running',?)", (task_id, name, now()))
    return task_id


def finish_task(path: Path, task_id: str, status: str, message: str) -> None:
    """写入任务结果；参数：路径、任务 ID、状态、说明；返回：无。"""
    if not isinstance(path,Path):
        path.apply_operation({'op':'finish','database':path.settings.database,'task_id':task_id,'event_key':uuid4().hex,'time':now(),
                              'event_type':'task_finished','state':status,'message':message})
        return
    with connect(path) as connection:
        connection.execute("UPDATE tasks SET status=?,finished_at=?,message=? WHERE id=?", (status, now(), message, task_id))


def recover_interrupted(path: Path, execution_key=None, is_alive=None) -> None:
    """在持有店铺锁时标记遗留任务；参数：数据库路径；返回：无。"""
    if not isinstance(path,Path):
        return path.recover_interrupted(execution_key,is_alive)
    with connect(path) as connection:
        connection.execute("UPDATE tasks SET status='needs_attention',finished_at=?,message=? WHERE status='running' OR (status='needs_attention' AND finished_at IS NULL)", (now(), "上次执行异常中断，请核对页面结果后重新运行"))


def update_task_progress(path: Path, task_id: str, status: str, message: str) -> None:
    """更新仍在执行的任务，不写入结束时间。"""
    if status not in {"running", "needs_attention"}:
        raise ValueError("执行中状态只能是 running 或 needs_attention")
    if not isinstance(path,Path):
        from ecommerce.infrastructure.tasks.events import current_events
        session=current_events()
        if session is None or session.task_id!=task_id:
            raise RuntimeError('MySQL 中途状态须通过当前任务的持久流水写入')
        session.progress(status,message)
        return
    with connect(path) as connection:
        connection.execute("UPDATE tasks SET status=?,message=? WHERE id=? AND finished_at IS NULL", (status, message, task_id))


def list_tasks(path: Path, limit: int = 50) -> list[dict]:
    """读取近期记录；参数：数据库路径、数量；返回：任务字典列表。"""
    if not isinstance(path,Path):
        return path.list_tasks(limit)
    with connect(path) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM tasks ORDER BY started_at DESC,rowid DESC LIMIT ?", (limit,))]


def task_counts(path: Path) -> dict[str, int]:
    """统计全部任务；参数：数据库路径；返回：状态与数量。"""
    if not isinstance(path,Path):
        return path.task_counts()
    with connect(path) as connection:
        return {row[0]: row[1] for row in connection.execute("SELECT status,COUNT(*) FROM tasks GROUP BY status")}
