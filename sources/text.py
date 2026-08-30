"""HTML -> 纯文本：Reddit RSS / HN 评论等场景共用，无第三方依赖。"""

import re
from html.parser import HTMLParser


class _TextExtract(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "table", "blockquote",
              "h1", "h2", "h3", "h4", "pre", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html):
    """块级标签转换行、合并空白、去掉实体；解析失败时尽力返回已提取部分。"""
    parser = _TextExtract()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        pass
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
