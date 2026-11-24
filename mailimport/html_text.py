import re
from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    hidden_tags = {"head", "iframe", "noscript", "object", "script", "style", "svg", "template"}
    block_tags = {
        "address", "article", "blockquote", "br", "div", "h1", "h2", "h3", "h4",
        "li", "p", "section", "table", "tr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden_tags:
            self.hidden.append(tag)
        elif not self.hidden:
            self._add_boundary(tag)

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        else:
            self._add_boundary(tag)

    def handle_startendtag(self, tag, attrs):
        if not self.hidden:
            self._add_boundary(tag)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(re.sub(r"[\t\n\f\r ]+", " ", data))

    def _add_boundary(self, tag):
        if tag in self.block_tags:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")


def html_to_text(html: str) -> str:
    """Read visible text without retaining HTML markup or its attributes."""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return "".join(extractor.parts)
