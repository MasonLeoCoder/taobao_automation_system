# 公共图片处理

`convert_image(data, output_format='png')`返回`ConvertedImage(data, extension, mimetype, reused)`。输出格式以真实内容为准，不依赖输入扩展名。可显示问题使用`ImageConversionError`，调用方可补充具体文件名称；不要将原件覆盖成转换结果。

本层依赖Pillow，不依赖Web、商品、数据库、AI。批量命名、临时输出和ZIP属于`modules/image_tools`。

Pillow官方依据：[EXIF方向校正](https://pillow.readthedocs.io/en/stable/reference/ImageOps.html#PIL.ImageOps.exif_transpose)、[图片格式](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html)。
