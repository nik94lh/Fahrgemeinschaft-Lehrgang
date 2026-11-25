import re
from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    hidden_tags = {"head", "iframe", "noscript", "object", "script", "style", "svg", "template"}
    block_tags = {
        "address", "article", "blockquote", "div", "h1", "h2", "h3", "h4",
        "li", "p", "section", "table", "tr",
    }
    cell_tags = {"td", "th"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []
        self.blocks: list[tuple[str, int]] = []
        self.visible_runs = 0
        self.line_has_text = False

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden_tags:
            self.hidden.append(tag)
        elif self.hidden:
            return
        elif tag in self.block_tags:
            self._end_line()
            self.blocks.append((tag, self.visible_runs))
        elif tag == "br":
            self._explicit_break()
        elif tag == "hr":
            self._end_line()
            self._explicit_break()
        elif tag in self.cell_tags:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in self.block_tags:
            if self.blocks and self.blocks[-1][0] == tag:
                _, opened_at = self.blocks.pop()
                if opened_at == self.visible_runs:
                    self._explicit_break()
            self._end_line()
        elif tag in self.cell_tags:
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        if not self.hidden and tag not in self.hidden_tags:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.hidden:
            return
        visible = re.sub(r"[\t\n\f\r ]+", " ", data)
        if visible.strip():
            self.visible_runs += 1
            self.line_has_text = True
        if self.line_has_text:
            self.parts.append(visible)

    def _end_line(self):
        if self.line_has_text:
            self.parts.append("\n")
            self.line_has_text = False

    def _explicit_break(self):
        self.parts.append("\n")
        self.visible_runs += 1
        self.line_has_text = False


def html_to_text(html: str) -> str:
    """Read visible text without retaining HTML markup or its attributes."""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return "".join(extractor.parts)
