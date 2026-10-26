"""按列表批次与位置读取缩略图，不借用其他商品或历史批次图片。"""
import sqlalchemy as sa


def thumbnail(connection, tables, row):
    """补充图片索引；参数：连接、表、观察行；返回：无，原位填充缩略图及说明。"""
    media, files = tables['media_references'], tables['file_assets']
    reference = connection.execute(sa.select(media).where(media.c.batch_id == row['batch_id'],
        media.c.position == row['slot_index'], media.c.role == 'list_image').order_by(media.c.id).limit(1)).mappings().first()
    row['thumbnail'] = None
    row['thumbnail_file'] = None
    if reference and reference['file_asset_id']:
        asset = connection.execute(sa.select(files).where(files.c.id == reference['file_asset_id'])).mappings().first()
        if asset:
            row['thumbnail_file'] = dict(asset)
            if asset['availability'] == 'present':
                row['thumbnail'] = asset['id']
        row['thumbnail_message'] = '本地图片不可用'
    else:
        status = reference['download_status'] if reference else None
        row['thumbnail_message'] = '图片下载失败' if status == 'failed' else '图片尚未下载' if reference and reference['source_url'] else '未获取图片地址'
