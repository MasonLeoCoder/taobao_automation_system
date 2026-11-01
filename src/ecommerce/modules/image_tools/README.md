# 图片工具业务

统一服务工具箱上传和商品主图下载，无Flask依赖、不写入采集或分析记录。

- `service.convert_uploads`：接受文件名/二进制流列表和目标格式；一次1～20张，单张10 MiB，合计50 MiB。
- `main_images.export_main_images`：先核对商品/已发布主图批次，再按可选媒体编号导出。整批最多200张，独立于页面20条分页；原件限制与上传相同。
- `exports.package_images`：单张输出图片，多张或显式整批输出ZIP；净化文件名、同名不区分大小写去重。输出总计最多150 MiB，8 MiB以上自动转临时磁盘文件。调用方必须关闭返回的stream，失败立即关闭并不返回部分包。

公共编码位于 `infrastructure/images/converter.py`：只接受字节和格式，处理真实编码、透明度及EXIF方向；不读取商品或网页状态。图片解码上限2500万像素，当前支持静态JPEG/PNG/WebP，输出PNG/JPG/original。original逐字节保留；同编码且方向无需修正时复用字节。转换副本不缩放；EXIF校正可能交换宽高。

Web上传和下载在 `web/routes/image_tools.py`，页面在 `web/templates/tools/`，布局在 `web/static/image_tools.css`；控件外观由公共form_controls维护。工具箱按需增加工具入口，不引入插件或新服务。
