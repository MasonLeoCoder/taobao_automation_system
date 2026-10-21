"""用户确认的广告推荐位标识；只检查单商品 DOM，不加载标识图片。"""

AD_RECOMMENDATION_IMAGE_URL = (
    "https://img.alicdn.com/imgextra/i3/"
    "O1CN01njPHBL1iDAvy7mi2J_!!6000000004378-2-tps-42-27.png"
)

# 在单商品范围内按图片地址判断存在，隐藏副本也算命中；不限制动态类名后缀。
AD_RECOMMENDATION_IMAGE_SELECTOR = f'img[src="{AD_RECOMMENDATION_IMAGE_URL}"]'
AD_RECOMMENDATION_MAIN_SELECTOR = (
    '[class*="mainPicAdaptWrapper--"] img[class*="mainP4pPic--"]'
    f'[src="{AD_RECOMMENDATION_IMAGE_URL}"]'
)
