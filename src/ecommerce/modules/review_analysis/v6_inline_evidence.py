"""只读拆分安全HTML中的混排引文，保留分析、列表结构和行内排版。"""
from dataclasses import dataclass,field
import html
from html.parser import HTMLParser
from ecommerce.modules.review_analysis import v6_checks


INLINE={'a','strong','em','br'}
VOID={'br','hr','img','input','meta','link'}


@dataclass
class Element:
    """保存已过滤标签及子节点，不接收或生成模型提供的新属性。"""
    tag:str=''
    opening:str=''
    closing:str=''
    children:list=field(default_factory=list)

    @property
    def raw(self):
        """还原原安全HTML；参数：无；返回：文字。"""
        return self.opening+''.join(raw(child) for child in self.children)+self.closing

    @property
    def text(self):
        """取得可见文字，换行标签占一字符；参数：无；返回：文字。"""
        return '\n' if self.tag=='br' else ''.join(text(child) for child in self.children)


def raw(node):
    """读取原片段；参数：元素或实体文字；返回：安全HTML。"""
    return node.raw if isinstance(node,Element) else node


def text(node):
    """读取解码文字；参数：元素或实体文字；返回：可见文字。"""
    return node.text if isinstance(node,Element) else html.unescape(node)


class Tree(HTMLParser):
    """解析已安全渲染的HTML，仅用于保留标签边界及文字定位。"""
    def __init__(self,rendered):
        """初始化树；参数：安全HTML；返回：解析器。"""
        super().__init__(convert_charrefs=False);self.root=Element();self.stack=[self.root]
        self.feed(rendered);self.close()

    def handle_starttag(self,tag,attrs):
        """保留过滤后的标签；参数：标签及属性；返回：无。"""
        node=Element(tag=tag,opening=self.get_starttag_text());self.stack[-1].children.append(node)
        if tag not in VOID:self.stack.append(node)

    def handle_startendtag(self,tag,attrs):
        """保留自闭合标签；参数：标签及属性；返回：无。"""
        self.handle_starttag(tag,attrs)
        if tag not in VOID:self.stack.pop()

    def handle_endtag(self,tag):
        """记录闭合标签；参数：标签；返回：无。"""
        if len(self.stack)>1 and self.stack[-1].tag==tag:self.stack.pop().closing='</'+tag+'>'

    def handle_data(self,value):
        """保留文字；参数：已过滤文字；返回：无。"""
        self.stack[-1].children.append(value)

    def handle_entityref(self,name):
        """保留命名实体；参数：实体名；返回：无。"""
        self.handle_data('&'+name+';')

    def handle_charref(self,name):
        """保留数字实体；参数：实体名；返回：无。"""
        self.handle_data('&#'+name+';')


def fragment(nodes,start,end):
    """按可见文字范围截取并重新闭合行内标签；参数：节点、起止位置；返回：安全HTML。"""
    parts=[];offset=0
    for node in nodes:
        length=len(text(node));left=max(0,start-offset);right=min(length,end-offset)
        if left<right:
            if left==0 and right==length:parts.append(raw(node))
            elif isinstance(node,Element):parts.append(node.opening+fragment(node.children,left,right)+node.closing)
            else:parts.append(html.escape(text(node)[left:right],quote=False))
        offset+=length
    return ''.join(parts)


def disclosure(body,count):
    """生成统一默认关闭的依据控件；参数：安全正文、引用数；返回：安全HTML。"""
    return '<details class="ui-report-evidence"><summary>查看原文依据（'+str(count)+'处引用）</summary><div class="ui-report-evidence-body">'+evidence_rows(body)+'</div></details>'


def evidence_rows(body):
    """把纯引文段落按每处引用分行，保留字词、标点和链接；参数：安全HTML；返回：展示HTML。"""
    parts=[]
    for node in Tree(body).root.children:
        if not isinstance(node,Element) or node.tag!='p' or not all(inline(child) for child in node.children):
            parts.append(raw(node));continue
        visible=node.text;spans=[];cursor=0
        for marker in v6_checks.MARKER.finditer(visible):
            if marker.start()<cursor:continue
            identity=v6_checks.metadata(marker[1]);quote,end=v6_checks.quoted(visible,marker.end())
            if visible[cursor:marker.start()].strip(' \t\r\n。；;，,') or identity is None or quote is None:break
            if 'href="#review-'+str(identity['review_id'])+'"' not in fragment(node.children,marker.start(),marker.end()):break
            if spans:spans[-1]=(spans[-1][0],marker.start())
            spans.append((marker.start() if spans else 0,end));cursor=end
        else:
            if spans and not visible[cursor:].strip(' \t\r\n。；;，,'):
                # 分隔标点及原空白留在所属条目中，不改写或丢弃原引文。
                spans[-1]=(spans[-1][0],len(visible))
                parts.extend('<p class="ui-report-evidence-item">'+fragment(node.children,start,end)+'</p>' for start,end in spans)
                continue
        parts.append(node.raw)
    return ''.join(parts)


def split_inline(nodes):
    """仅拆出有明确标签、完整引号与链接的引文；参数：行内节点；返回：HTML或None。"""
    visible=''.join(text(node) for node in nodes);groups=[];consumed=0
    for marker in v6_checks.MARKER.finditer(visible):
        if marker.start()<consumed:continue
        identity=v6_checks.metadata(marker[1]);quote,end=v6_checks.quoted(visible,marker.end())
        # 句中紧贴文字的标签可能属于分析句，不猜测拆分边界。
        if marker.start()!=consumed and marker.start() and visible[marker.start()-1] not in ' \t\r\n：:。；;，,！!？?':continue
        if identity is None or quote is None:continue
        marker_html=fragment(nodes,marker.start(),marker.end())
        if 'href="#review-'+str(identity['review_id'])+'"' not in marker_html:continue
        if groups and not visible[groups[-1][1]:marker.start()].strip(' \t\r\n。；;，,'):
            groups[-1][1]=end;groups[-1][2]+=1
        else:groups.append([marker.start(),end,1])
        consumed=end
    if not groups:return None
    parts=[];cursor=0
    for start,end,count in groups:
        if visible[cursor:start].strip():parts.append('<p>'+fragment(nodes,cursor,start)+'</p>')
        parts.append(disclosure('<p>'+fragment(nodes,start,end)+'</p>',count));cursor=end
    if visible[cursor:].strip():parts.append('<p>'+fragment(nodes,cursor,len(visible))+'</p>')
    return ''.join(parts)


def inline(node):
    """确认可安全分割的行内结构；参数：节点；返回：布尔值。"""
    return not isinstance(node,Element) or node.tag in INLINE and all(inline(child) for child in node.children)


def transform(node):
    """递归处理段落和各层列表项，保留未知块；参数：节点；返回：安全HTML。"""
    if not isinstance(node,Element):return node
    if node.tag in {'details','pre','code','table'}:return node.raw
    if node.tag=='p' and all(inline(child) for child in node.children):
        return split_inline(node.children) or node.raw
    if node.tag=='li':
        parts=[];run=[]
        def flush():
            """处理列表内一段行内内容；参数：无；返回：无。"""
            if run:parts.append(split_inline(run) or ''.join(raw(child) for child in run));run.clear()
        for child in node.children:
            if inline(child):run.append(child)
            else:flush();parts.append(transform(child))
        flush();return node.opening+''.join(parts)+node.closing
    return node.opening+''.join(transform(child) for child in node.children)+node.closing


def collapse(rendered):
    """将混排及列表引用转为统一折叠；参数：安全HTML；返回：仅展示用HTML。"""
    return transform(Tree(rendered).root)
