"""本机执行资源和进程身份；不只凭进程编号或等待时间判断任务已失效。"""
from datetime import datetime,timezone
import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4


def process_started_at(pid):
    """读取进程实际创建时间；参数：进程编号；返回：UTC ISO 时间，无法访问为空。"""
    if not isinstance(pid,int) or pid<=0:
        return None
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            return None
        try:
            times=[wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle,*[ctypes.byref(t) for t in times]):
                return None
            ticks=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
            return datetime.fromtimestamp((ticks-116444736000000000)/10000000,timezone.utc).isoformat(timespec='microseconds')
        finally:
            kernel.CloseHandle(handle)
    try:
        fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        boot=next(float(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
        return datetime.fromtimestamp(boot+float(fields[19])/os.sysconf('SC_CLK_TCK'),timezone.utc).isoformat(timespec='microseconds')
    except (OSError,ValueError,StopIteration):
        return None


def owner_alive(pid,started_at):
    """核对进程是否仍为原进程；参数：编号及启动时间；返回：真、假或无法确认的空值。"""
    if not pid or not started_at:
        return None
    actual=process_started_at(pid)
    if actual:
        return datetime.fromisoformat(actual)==datetime.fromisoformat(started_at)
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        handle=kernel.OpenProcess(0x1000,False,pid)
        if handle:
            kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            kernel.CloseHandle(handle)
            return None
        return False if ctypes.get_last_error()==87 else None
    try:
        os.kill(pid,0)
        return None
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


def execution_key(settings):
    """计算沿用项目串行锁的资源键；参数：配置；返回：SHA-256。"""
    return hashlib.sha256(str(settings.profile_path.resolve()).casefold().encode('utf-8')).hexdigest()


def atomic_json(path,value):
    """原子持久化本地状态；短暂文件占用最多尝试3次，其他失败原样抛出。"""
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name('.'+path.name+'-'+uuid4().hex+'.tmp')
    try:
        with temporary.open('w',encoding='utf-8',newline='\n') as handle:
            json.dump(value,handle,ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(3):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt==2:
                    raise
                time.sleep(.02*(attempt+1))
    finally:
        temporary.unlink(missing_ok=True)


def active_local_tasks(settings):
    """读取仍存活且属于当前资源的本地任务；参数：配置；返回：可用于断库控制的描述列表。"""
    rows=[]
    for path in (settings.workspace/'storage/task_controls').glob('*.active.json'):
        try:
            row=json.loads(path.read_text(encoding='utf-8'))
            import re
            if not re.fullmatch('[a-f0-9]{32}',row.get('id','')) or row.get('execution_key')!=execution_key(settings):
                continue
            if row.get('database')!=settings.mysql.database or owner_alive(row.get('owner_pid'),row.get('owner_process_started_at')) is not True:
                continue
            rows.append(row)
        except (OSError,ValueError,TypeError):
            continue
    return rows
