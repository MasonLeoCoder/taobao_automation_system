"""采集数据的不可变流式快照；原件与规范化记录分别保留，恢复不调用页面。"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from ecommerce.shared.exceptions import NeedsAttention

FORMAT_VERSION=1


def digest_file(path):
    """流式计算文件哈希；参数：文件路径；返回：SHA-256。"""
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle,'sha256').hexdigest()


def owned_path(root,path):
    """限定可访问文件；参数：根目录及候选路径；返回：根内绝对路径。"""
    root=Path(root).resolve()
    path=Path(path)
    candidate=(root/path).resolve() if not path.is_absolute() else path.resolve()
    if candidate==root or root not in candidate.parents:
        raise ValueError('采集文件路径超出工作目录')
    return candidate


def file_record(workspace,path,*,expected_sha=None,expected_size=None,**attributes):
    """核对原文件；参数：根、路径、可选原校验值及属性；返回：相对文件索引。"""
    path=owned_path(workspace,path)
    relative=path.relative_to(Path(workspace).resolve()).as_posix()
    if not path.is_file():
        return {'relative_path':relative,'availability':'missing','sha256':expected_sha,'size_bytes':expected_size,**attributes}
    digest=digest_file(path)
    size=path.stat().st_size
    if (expected_sha is not None and digest!=expected_sha) or (expected_size is not None and size!=expected_size):
        raise ValueError('已登记的采集文件内容不符：'+relative)
    return {'relative_path':relative,'availability':'present','sha256':digest,'size_bytes':size,**attributes}


def save_snapshot(settings,header,records,*,directory=None):
    """流式保存规范化记录；参数：配置、头、记录迭代器和目录；返回：带哈希的快照引用。"""
    directory=Path(directory or settings.workspace/'storage/collection_snapshots').resolve()
    owned_path(settings.workspace,directory/'placeholder')
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/(uuid4().hex+'.jsonl')
    count=0
    try:
        with path.open('xb') as handle:
            handle.write((json.dumps({'format_version':FORMAT_VERSION,**header},ensure_ascii=False,
                                    sort_keys=True,separators=(',',':'))+'\n').encode('utf-8'))
            for record in records:
                handle.write((json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode('utf-8'))
                count+=1
            handle.flush()
            os.fsync(handle.fileno())
        return {'relative_path':path.relative_to(settings.workspace.resolve()).as_posix(),
                'sha256':digest_file(path),'size_bytes':path.stat().st_size,'record_count':count}
    except (OSError,ValueError,TypeError):
        path.unlink(missing_ok=True)
        raise


def open_snapshot(settings,reference):
    """验证固定快照并逐行读取；参数：配置、引用；返回：头和记录迭代器。"""
    path=owned_path(settings.workspace,reference['relative_path'])
    if path.stat().st_size!=reference['size_bytes'] or digest_file(path)!=reference['sha256']:
        raise NeedsAttention('采集快照校验失败，未跳过或覆盖数据')
    def records():
        """读取完整 JSON 行；参数：无；返回：记录迭代。"""
        with path.open('rb') as handle:
            for line in handle:
                if not line.endswith(b'\n'):
                    raise NeedsAttention('采集快照末条不完整')
                yield json.loads(line)
    iterator=records()
    header=next(iterator)
    if header.get('format_version')!=FORMAT_VERSION:
        iterator.close()
        raise NeedsAttention('采集快照格式版本不支持')
    return header,iterator


def sqlite_records(path,query,parameters=()):
    """一致性只读遍历模块工作库；参数：路径、内部查询及参数；返回：行迭代。"""
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as connection:
        connection.execute('BEGIN')
        yield from connection.execute(query,parameters)


def json_array(path,key):
    """按记录读取顶层 JSON 数组；参数：导出文件、数组键；返回：对象迭代，内存不随总条数增长。"""
    decoder=json.JSONDecoder()
    with Path(path).open('r',encoding='utf-8') as source:
        buffer=''
        position=0
        ended=False
        def fill():
            """继续读取并丢弃已消费前缀；参数：无；返回：无。"""
            nonlocal buffer,position,ended
            chunk=source.read(65536)
            buffer=buffer[position:]+chunk
            position=0
            ended=not chunk
        def whitespace():
            """移动到下一非空字符；参数：无；返回：无。"""
            nonlocal position
            while True:
                while position<len(buffer) and buffer[position].isspace():
                    position+=1
                if position<len(buffer) or ended:
                    return
                fill()
        def take(character):
            """消费一个结构字符；参数：字符；返回：无。"""
            nonlocal position
            whitespace()
            if position>=len(buffer) or buffer[position]!=character:
                raise ValueError('采集明细 JSON 结构无效')
            position+=1
        def value():
            """解码一个对象；参数：无；返回：JSON 值。"""
            nonlocal position
            whitespace()
            while True:
                try:
                    result,last=decoder.raw_decode(buffer,position)
                    # 数字等标量若位于块尾，须读取下个块后再决定词尾。
                    if last==len(buffer) and not ended:
                        fill()
                        continue
                    position=last
                    return result
                except json.JSONDecodeError:
                    if ended:
                        raise ValueError('采集明细 JSON 未完整保存') from None
                    fill()
        take('{')
        found=False
        needs_property=False
        while True:
            whitespace()
            if position<len(buffer) and buffer[position]=='}':
                if needs_property:
                    raise ValueError('采集明细对象有尾随逗号')
                position+=1
                break
            name=value()
            if not isinstance(name,str):
                raise ValueError('采集明细对象键必须为文本')
            needs_property=False
            take(':')
            if name==key:
                if found:
                    raise ValueError('采集明细重复声明数组')
                found=True
                take('[')
                needs_item=False
                while True:
                    whitespace()
                    if position<len(buffer) and buffer[position]==']':
                        if needs_item:
                            raise ValueError('采集明细数组有尾随逗号')
                        position+=1
                        break
                    yield value()
                    needs_item=False
                    whitespace()
                    if position<len(buffer) and buffer[position]==',':
                        position+=1
                        needs_item=True
                    elif position>=len(buffer) or buffer[position]!=']':
                        raise ValueError('采集明细数组分隔符无效')
            else:
                value()
            whitespace()
            if position<len(buffer) and buffer[position]==',':
                position+=1
                needs_property=True
            elif position>=len(buffer) or buffer[position]!='}':
                raise ValueError('采集明细对象分隔符无效')
        whitespace()
        if not found or position<len(buffer):
            raise ValueError('采集明细缺少指定数组或存在尾随内容')
