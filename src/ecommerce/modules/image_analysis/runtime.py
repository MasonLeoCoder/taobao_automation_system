"""图片请求适配共享预算与后台执行，无自动重试。"""
from ecommerce.modules.composite_analysis import runtime as shared
from . import planner,report


def execute(settings,task_id,events,client_factory):
    """将已预检图片发送至视觉接口，文本汇总同样使用冻结视觉能力。"""
    class Client:
        """将共享执行方法适配到显式视觉接口。"""
        def __init__(self,config,**options):
            """保存工厂创建的客户端；参数：模型及配置。"""
            self.client=client_factory(config,**options)

        def complete_messages(self,messages,**options):
            """只走图片专用契约；参数：真实消息；返回：响应。"""
            return self.client.complete_images(messages,**options)
    return shared.execute(settings,task_id,events,Client,planning=planner,reporting=report)


def recheck(settings,data,report_id):
    """本地定位重查零AI调用，旧版本保留。"""
    return shared.recheck(settings,data,report_id,reporting=report)
