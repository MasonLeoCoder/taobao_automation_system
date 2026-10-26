"""本机后台任务的持久派发描述；业务结果仍以统一任务和采集数据库为准。"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from ecommerce.infrastructure.tasks.ownership import atomic_json, owner_alive
from ecommerce.infrastructure.storage.database import now


def directory(settings):
    """返回派发目录；参数：配置；返回：工作空间内的目录。"""
    return settings.workspace / 'storage/web_collection_jobs'


def read(settings, identifier):
    """读取指定派发；参数：配置、编号；返回：原始记录，不接受路径输入。"""
    if not isinstance(identifier,str) or not re.fullmatch('[a-f0-9]{32}',identifier):
        raise ValueError('采集任务编号无效')
    try:
        return json.loads((directory(settings)/f'{identifier}.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise LookupError('采集任务不存在') from None


def save(settings, row):
    """持久保存派发状态；参数：配置、记录；返回：无。"""
    row['updated_at']=now()
    atomic_json(directory(settings)/f"{row['id']}.json",row)


def rows(settings):
    """读取本配置的派发历史；参数：配置；返回：按创建时间倒序的记录。"""
    result=[]
    for path in directory(settings).glob('*.json'):
        row=read(settings,path.stem)
        if row.get('database')==settings.mysql.database:
            result.append(row)
    return sorted(result,key=lambda row:(row['created_at'],row['id']),reverse=True)


def alive(row):
    """核对后台进程归属；参数：派发；返回：真、假或无法核实。"""
    return owner_alive(row.get('owner_pid'),row.get('owner_process_started_at'))


def launch(settings, identifier):
    """启动无终端后台进程；参数：配置、派发编号；返回：进程对象，不执行浏览器逻辑。"""
    log=directory(settings)/f'{identifier}.log'
    with log.open('ab') as output:
        return subprocess.Popen([sys.executable,'-m','ecommerce.modules.collection.web_worker',
                                 str(settings.workspace.resolve()),identifier],
                                cwd=settings.root,stdin=subprocess.DEVNULL,stdout=output,stderr=output,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
                                start_new_session=os.name!='nt')
