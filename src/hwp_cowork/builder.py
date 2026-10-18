"""서식 명세 → HWPX. 공문서·보고서를 처음부터 제대로 꾸며 만든다.

rhwp scaffold 는 제목·문단·표의 뼈대만 만들고 글꼴·굵기·색·정렬·셀 음영을 줄 수 없다. 서식 명령(apply-char-format …)을
수백 번 잇는 것은 느리고 글꼴을 늘릴 수도 없다. 그래서 scaffold 가 만든 HWPX 를 **포장(mimetype·manifest·settings)만**
빌려 쓰고, header.xml 의 참조 목록(글꼴·테두리/배경·글자모양·문단모양·탭)과 section0.xml 본문을 여기서 새로 쓴다.
결과는 rhwp 로 다시 읽어 검증한다 (server 쪽).

명세 (모든 키는 선택, 단위: 글자 pt · 길이 mm · 줄간격 %):

  {"page": {"margin": [위, 아래, 왼, 오른], "header": "머리말 글", "page_number": "- n -" | "n" | false,
            "landscape": false},
   "fonts": {"body": "휴먼명조", "head": "HY헤드라인M", "table": "맑은 고딕"},   # 이름 또는 블록의 font 로 직접
   "base": {"size": 13, "line": 160},
   "blocks": [ … ]}

블록:
  {"p": "글" | [런…], "style": "title|subtitle|h1|h2|h3|l1|l2|l3|l4|note|body|center|right|date|sign",
   "size", "bold", "italic", "underline", "color", "shade", "font", "spacing"(자간 %),
   "align": "left|center|right|justify|distribute", "indent"(왼 여백 mm), "hanging"(내어쓰기 mm),
   "before"/"after"(pt), "line"(%), "page_break": true}
     런: {"t": "글", "b", "i", "u", "color", "size", "font", "shade", "spacing"}  — "\n" 은 줄바꿈
  {"table": [[칸…]…], "widths": [비율…], "header": 1, "head_fill": "#D9D9D9", "align": ["center","left",…],
   "size": 11, "font", "merge": [[행,열,행수,열수]…], "border": "grid|rows|none", "line": "#000000",
   "outer": "thick|normal", "row_height": mm, "heights": [행별 mm…], "width": 100(%), "pad": [왼,오른,위,아래 mm](칸 안 여백)}
     칸: "글" 또는 {"t"|"p"(런 목록), "fill", "align", "b", "color", "size", "valign": "top|center|bottom",
          "line_l"|"line_r"|"line_t"|"line_b": "double|dash|dot|none" (그 칸 한 변만)}
  {"rule": "#4472C4", "thick": 2}                — 가로 띠 (표지 위아래 굵은 선)
  {"box": "글" | [블록…], "fill": "#F2F2F2", "line": "#000000", "border": "solid|double|dash|dot|none", "style"…}
  {"toc": [["Ⅰ. 개요", "3"], …], "size", "bold"} — 점선 채움 + 오른쪽 쪽 번호
  {"space": pt}                                   — 빈 줄 높이
  {"page_break": true}
예전 scaffold 형식({"type":"heading|paragraph|table", …})도 받는다.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import escape

MM = 283.465          # HWPUNIT / mm
PT = 100              # HWPUNIT / pt
PAGE_W, PAGE_H = 59528, 84188
LANGS = ("HANGUL", "LATIN", "HANJA", "JAPANESE", "OTHER", "SYMBOL", "USER")
ALIGN = {"left": "LEFT", "center": "CENTER", "right": "RIGHT", "justify": "JUSTIFY", "distribute": "DISTRIBUTE",
         "both": "JUSTIFY"}
LINE = {"solid": "SOLID", "double": "DOUBLE_SLIM", "dash": "DASH", "dot": "DOT", "none": "NONE"}
VALIGN = {"top": "TOP", "center": "CENTER", "middle": "CENTER", "bottom": "BOTTOM"}

DEFAULT_FONTS = {"body": "휴먼명조", "head": "HY헤드라인M", "table": "맑은 고딕", "gothic": "맑은 고딕"}

# 공문서 관례: □ 15pt → ❍ 15pt(2칸) → - 14pt(4칸) → · 13pt. 내어쓰기로 둘째 줄이 기호 뒤에 맞는다.
STYLES: dict[str, dict] = {
    "title":    {"size": 22, "bold": True, "font": "head", "align": "center", "after": 6, "spacing": 10},
    "subtitle": {"size": 15, "bold": True, "font": "head", "align": "center", "after": 6},
    "h1":       {"size": 16, "bold": True, "font": "head", "before": 14, "after": 6},
    "h2":       {"size": 15, "bold": True, "font": "head", "before": 10, "after": 4},
    "h3":       {"size": 14, "bold": True, "font": "gothic", "before": 6, "after": 2},
    "l1":       {"size": 15, "indent": 0, "hanging": 5.5, "before": 6},
    "l2":       {"size": 15, "indent": 3, "hanging": 5.5, "before": 3},
    "l3":       {"size": 14, "indent": 7, "hanging": 4, "before": 2},
    "l4":       {"size": 13, "indent": 10, "hanging": 3.5},
    "note":     {"size": 12, "indent": 3, "hanging": 4.5, "color": "#333333"},
    "body":     {},
    "center":   {"align": "center"},
    "right":    {"align": "right"},
    "date":     {"size": 15, "align": "center", "before": 12},
    "sign":     {"size": 20, "bold": True, "font": "head", "align": "center", "before": 18, "spacing": 30},
    "caption":  {"size": 11, "align": "right", "color": "#333333"},
}


class SpecError(ValueError):
    pass


def _color(c: Any, default: str = "#000000") -> str:
    if c in (None, "", False):
        return default
    s = str(c).strip()
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", s):
        return "#" + s.lstrip("#").upper()
    named = {"black": "#000000", "white": "#FFFFFF", "red": "#C00000", "blue": "#1F4E9A", "navy": "#1F3864",
             "gray": "#808080", "grey": "#808080", "lightgray": "#D9D9D9", "green": "#38761D"}
    if s.lower() in named:
        return named[s.lower()]
    raise SpecError(f"색 형식이 이상하다: {c!r} (#RRGGBB)")


def _num(v: Any, what: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        raise SpecError(f"{what} 는 숫자여야 한다: {v!r}") from None


# ---- 참조 목록 (글꼴·테두리·글자모양·문단모양) — 같은 모양은 하나로 ------------------------------------------

@dataclass
class Refs:
    fonts: list[str] = field(default_factory=list)
    border_fills: list[tuple] = field(default_factory=list)       # 1부터
    char_prs: list[tuple] = field(default_factory=list)
    para_prs: list[tuple] = field(default_factory=list)
    tab_prs: list[tuple] = field(default_factory=lambda: [()])     # 0 = 기본

    def font(self, name: str) -> int:
        if name not in self.fonts:
            self.fonts.append(name)
        return self.fonts.index(name)

    def border_fill(self, left="NONE", right="NONE", top="NONE", bottom="NONE", fill=None,
                    width=("0.12 mm",) * 4, color="#000000") -> int:
        key = (left, right, top, bottom, fill, tuple(width), color)
        if key not in self.border_fills:
            self.border_fills.append(key)
        return self.border_fills.index(key) + 1

    def char(self, size, font, bold, italic, underline, color, shade, spacing) -> int:
        key = (int(round(size * PT)), font, bool(bold), bool(italic), bool(underline), color, shade, int(spacing))
        if key not in self.char_prs:
            self.char_prs.append(key)
        return self.char_prs.index(key)

    def para(self, align, left, intent, before, after, line, tab=0, keep_next=False, page_break=False,
             border=1, char_wrap=False) -> int:
        key = (align, int(left), int(intent), int(before), int(after), int(line), tab, keep_next, page_break,
               border, char_wrap)
        if key not in self.para_prs:
            self.para_prs.append(key)
        return self.para_prs.index(key)

    def tab_right_dot(self, pos: int) -> int:
        key = (pos,)
        if key not in self.tab_prs:
            self.tab_prs.append(key)
        return self.tab_prs.index(key)

    # -- XML
    def xml(self) -> str:
        ff = "".join(
            f'<hh:fontface lang="{lang}" fontCnt="{len(self.fonts)}">' +
            "".join(f'<hh:font id="{i}" face="{escape(n, {chr(34): "&quot;"})}" type="TTF" isEmbedded="0"/>'
                    for i, n in enumerate(self.fonts)) + "</hh:fontface>" for lang in LANGS)
        out = [f'<hh:fontfaces itemCnt="{len(LANGS)}">{ff}</hh:fontfaces>']

        bfs = []
        for i, (l, r, t, b, fill, w, col) in enumerate(self.border_fills, 1):
            brush = (f'<hc:fillBrush><hc:winBrush faceColor="{fill}" hatchColor="#999999" alpha="0"/></hc:fillBrush>'
                     if fill else "")
            bfs.append(
                f'<hh:borderFill id="{i}" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">'
                '<hh:slash type="NONE" Crooked="0" isCounter="0"/><hh:backSlash type="NONE" Crooked="0" isCounter="0"/>'
                f'<hh:leftBorder type="{l}" width="{w[0]}" color="{col}"/>'
                f'<hh:rightBorder type="{r}" width="{w[1]}" color="{col}"/>'
                f'<hh:topBorder type="{t}" width="{w[2]}" color="{col}"/>'
                f'<hh:bottomBorder type="{b}" width="{w[3]}" color="{col}"/>'
                f'<hh:diagonal type="SOLID" width="0.1 mm" color="#000000"/>{brush}</hh:borderFill>')
        out.append(f'<hh:borderFills itemCnt="{len(bfs)}">{"".join(bfs)}</hh:borderFills>')

        cps = []
        for i, (h, font, bold, italic, under, color, shade, sp) in enumerate(self.char_prs):
            ids = " ".join(f'{k}="{font}"' for k in ("hangul", "latin", "hanja", "japanese", "other", "symbol", "user"))
            sps = " ".join(f'{k}="{sp}"' for k in ("hangul", "latin", "hanja", "japanese", "other", "symbol", "user"))
            same = lambda v: " ".join(f'{k}="{v}"' for k in ("hangul", "latin", "hanja", "japanese", "other", "symbol", "user"))  # noqa: E731
            cps.append(
                f'<hh:charPr id="{i}" height="{h}" textColor="{color}" shadeColor="{shade or "none"}" useFontSpace="0" '
                f'useKerning="0" symMark="NONE" borderFillIDRef="1"><hh:fontRef {ids}/><hh:ratio {same(100)}/>'
                f'<hh:spacing {sps}/><hh:relSz {same(100)}/><hh:offset {same(0)}/>'
                + ("<hh:italic/>" if italic else "") + ("<hh:bold/>" if bold else "") +
                f'<hh:underline type="{"BOTTOM" if under else "NONE"}" shape="SOLID" color="{color}"/>'
                '<hh:strikeout shape="NONE" color="#000000"/><hh:outline type="NONE"/>'
                '<hh:shadow type="NONE" color="#000000" offsetX="0" offsetY="0"/></hh:charPr>')
        out.append(f'<hh:charProperties itemCnt="{len(cps)}">{"".join(cps)}</hh:charProperties>')

        tps = []
        for i, t in enumerate(self.tab_prs):
            items = (f'<hp:switch><hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/HwpUnitChar">'
                     f'<hh:tabItem pos="{t[0]}" type="RIGHT" leader="DOT" unit="HWPUNIT"/></hp:case><hp:default>'
                     f'<hh:tabItem pos="{t[0]}" type="RIGHT" leader="DOT"/></hp:default></hp:switch>') if t else ""
            tps.append(f'<hh:tabPr id="{i}" autoTabLeft="0" autoTabRight="0">{items}</hh:tabPr>')
        out.append(f'<hh:tabProperties itemCnt="{len(tps)}">{"".join(tps)}</hh:tabProperties>')

        pps = []
        for i, (al, left, intent, before, after, line, tab, keep, pb, pbf, char_wrap) in enumerate(self.para_prs):
            def margin(f: int) -> str:     # HwpUnitChar 쪽은 값이 반 — scaffold 가 내는 그대로
                return (f'<hh:margin><hc:intent value="{intent // f}" unit="HWPUNIT"/><hc:left value="{left // f}" unit="HWPUNIT"/>'
                        f'<hc:right value="0" unit="HWPUNIT"/><hc:prev value="{before // f}" unit="HWPUNIT"/>'
                        f'<hc:next value="{after // f}" unit="HWPUNIT"/></hh:margin>'
                        f'<hh:lineSpacing type="PERCENT" value="{line}" unit="HWPUNIT"/>')
            pps.append(
                f'<hh:paraPr id="{i}" tabPrIDRef="{tab}" condense="0" fontLineHeight="0" snapToGrid="1" '
                f'suppressLineNumbers="0" checked="0"><hh:align horizontal="{al}" vertical="BASELINE"/>'
                '<hh:heading type="NONE" idRef="0" level="0"/>'
                f'<hh:breakSetting breakLatinWord="KEEP_WORD" breakNonLatinWord="{"KEEP_WORD" if char_wrap else "BREAK_WORD"}" widowOrphan="0" '
                f'keepWithNext="{int(keep)}" keepLines="0" pageBreakBefore="{int(pb)}" lineWrap="BREAK"/>'
                '<hh:autoSpacing eAsianEng="0" eAsianNum="0"/>'
                '<hp:switch><hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/HwpUnitChar">'
                f'{margin(2)}</hp:case><hp:default>{margin(1)}</hp:default></hp:switch>'
                f'<hh:border borderFillIDRef="{pbf}" offsetLeft="{400 if pbf > 1 else 0}" offsetRight="{400 if pbf > 1 else 0}" '
                f'offsetTop="{200 if pbf > 1 else 0}" offsetBottom="{200 if pbf > 1 else 0}" connect="0" ignoreMargin="0"/></hh:paraPr>')
        out.append(f'<hh:paraProperties itemCnt="{len(pps)}">{"".join(pps)}</hh:paraProperties>')
        return "".join(out)


# ---- 본문 ------------------------------------------------------------------------------------------------

class Builder:
    def __init__(self, spec: dict) -> None:
        if not isinstance(spec, dict):
            raise SpecError("spec 은 JSON 객체여야 한다.")
        self.spec = spec
        self.refs = Refs()
        self.refs.border_fill()                                   # 1 = 테두리 없음 (글자·문단 기본)
        fonts = {**DEFAULT_FONTS, **(spec.get("fonts") or {})}
        self.fonts = fonts
        base = spec.get("base") or {}
        self.base_size = _num(base.get("size", 13), "base.size")
        self.base_line = int(_num(base.get("line", 160), "base.line"))
        self.base_font = fonts["body"]
        self.refs.font(self.base_font)
        self.refs.char(self.base_size, 0, False, False, False, "#000000", None, 0)        # 0 = 바탕글
        self.refs.para("JUSTIFY", 0, 0, 0, 0, self.base_line)
        page = spec.get("page") or {}
        m = page.get("margin") or [20, 15, 20, 20]
        if not (isinstance(m, list) and len(m) == 4):
            raise SpecError("page.margin 은 [위, 아래, 왼, 오른] mm")
        self.margin = [int(_num(x, "page.margin") * MM) for x in m]
        self.landscape = bool(page.get("landscape"))
        pw = PAGE_H if self.landscape else PAGE_W
        self.text_w = pw - self.margin[2] - self.margin[3]
        self.page = page
        self._pid = 0
        self.toc_auto: list[str] = []          # 쪽을 자동으로 채울 목차 제목
        self.toc_pages: dict[str, str] = {}    # 두 번째 그리기에 쓸 제목 → 쪽
        self.restart: tuple[str, int] | None = None   # (쪽 번호를 새로 시작하는 문단 글, 시작 번호)

    # -- 글자·문단 모양
    def _font_id(self, f: str | None) -> int:
        name = self.fonts.get(f, f) if f else self.base_font
        return self.refs.font(name)

    def _style(self, b: dict) -> dict:
        st = b.get("style")
        if st and st not in STYLES:
            raise SpecError(f"모르는 style {st!r}. 있는 것: {sorted(STYLES)}")
        return {**STYLES.get(st or "body", {}), "style": st or "body",
                **{k: v for k, v in b.items() if k not in ("p", "t", "style")}}

    def _char(self, s: dict, run: dict | None = None) -> int:
        r = {**s, **(run or {})}
        if run:      # 런의 짧은 키
            for a, k in (("b", "bold"), ("i", "italic"), ("u", "underline")):
                if a in run:
                    r[k] = run[a]
        size = _num(r.get("size", self.base_size), "size")
        if not 1 <= size <= 200:
            raise SpecError(f"글자 크기가 이상하다: {size}")
        return self.refs.char(size, self._font_id(r.get("font")), r.get("bold"), r.get("italic"), r.get("underline"),
                              _color(r.get("color")), _color(r["shade"]) if r.get("shade") else None,
                              int(_num(r.get("spacing", 0), "spacing")))

    def _para(self, s: dict, tab: int = 0) -> int:
        al = ALIGN.get(str(s.get("align", "justify")).lower())
        if al is None:
            raise SpecError(f"align 은 {sorted(ALIGN)} 중 하나: {s.get('align')!r}")
        hang = _num(s.get("hanging", 0), "hanging") * MM
        left = _num(s.get("indent", 0), "indent") * MM + hang
        border = 1
        if s.get("bar"):
            left += 3 * MM          # 막대와 글 사이
        elif s.get("fill"):
            left += 3 * MM
        if s.get("fill") or s.get("bar") or s.get("line_below") or s.get("line_above"):
            bar = _color(s["bar"]) if s.get("bar") else None
            below = _color(s["line_below"]) if s.get("line_below") else None
            above = _color(s["line_above"]) if s.get("line_above") else None
            border = self.refs.border_fill(
                "SOLID" if bar else "NONE", "NONE", "SOLID" if above else "NONE", "SOLID" if below else "NONE",
                fill=_color(s["fill"]) if s.get("fill") else None,
                width=("1.5 mm" if bar else "0.12 mm", "0.12 mm", "0.4 mm", "0.4 mm"),
                color=bar or below or above or "#000000")
        return self.refs.para(al, left, -hang, _num(s.get("before", 0), "before") * PT,
                              _num(s.get("after", 0), "after") * PT, int(_num(s.get("line", self.base_line), "line")),
                              tab, bool(s.get("keep_next", s.get("style", "") in ("h1", "h2", "h3"))),
                              bool(s.get("page_break")), border, str(s.get("wrap", "")).lower() == "char")

    # -- XML 조각
    def _p(self, para_id: int, runs_xml: str, page_break=False) -> str:
        self._pid += 1
        return (f'<hp:p id="{self._pid}" paraPrIDRef="{para_id}" styleIDRef="0" pageBreak="{int(page_break)}" '
                f'columnBreak="0" merged="0">{runs_xml}</hp:p>')

    @staticmethod
    def _t(text: str) -> str:
        parts = str(text).split("\n")
        inner = "<hp:lineBreak/>".join(escape(p).replace("\t", "<hp:tab/>") for p in parts)
        return f"<hp:t>{inner}</hp:t>"

    def _runs(self, content: Any, s: dict) -> str:
        if content is None:
            content = ""
        if isinstance(content, (str, int, float)):
            content = [{"t": str(content)}]
        if not isinstance(content, list):
            raise SpecError(f"글은 문자열이나 런 목록이어야 한다: {content!r}"[:200])
        out = []
        for r in content:
            if isinstance(r, str):
                r = {"t": r}
            if not isinstance(r, dict) or "t" not in r:
                raise SpecError(f"런은 {{\"t\": 글, …}}: {r!r}"[:200])
            out.append(f'<hp:run charPrIDRef="{self._char(s, r)}">{self._t(r["t"])}</hp:run>')
        if not out or all(not (isinstance(r, dict) and r.get("t")) for r in content):
            out = [f'<hp:run charPrIDRef="{self._char(s)}"/>']
        return "".join(out)

    def paragraph(self, b: dict, extra_run: str = "", tab: int = 0) -> str:
        s = self._style(b)
        body = b.get("p", b.get("t", b.get("text", "")))
        if b.get("num"):          # 장 번호 상자: 색 바탕에 흰 굵은 글자 (예: " Ⅰ ")
            box = {"t": f" {b['num']} ", "b": True, "color": b.get("num_color", "#FFFFFF"),
                   "shade": b.get("num_fill", "#1F3864")}
            body = [box, {"t": "  "}] + (body if isinstance(body, list) else [{"t": str(body)}])
        if b.get("page_restart"):  # 이 문단이 있는 쪽부터 쪽 번호를 새로 (표지·목차 빼고 본문 1쪽)
            plain = body if isinstance(body, str) else "".join(str(x.get("t", "")) if isinstance(x, dict) else str(x)
                                                               for x in body)
            self.restart = (plain, int(b["page_restart"]))
            extra_run += (f'<hp:run charPrIDRef="{self._char(s)}"><hp:ctrl><hp:newNum num="{int(b["page_restart"])}" '
                          'numType="PAGE"/></hp:ctrl></hp:run>')
        return self._p(self._para(s, tab), extra_run + self._runs(body, s), bool(s.get("page_break")))

    # -- 표
    def table(self, b: dict) -> str:
        rows = b["table"]
        if not (isinstance(rows, list) and rows and all(isinstance(r, list) for r in rows)):
            raise SpecError("table 은 행 목록(칸 목록의 목록)이어야 한다.")
        nrow, ncol = len(rows), max(len(r) for r in rows)
        if ncol == 0 or ncol > 40 or nrow > 500:
            raise SpecError(f"표 크기가 이상하다: {nrow}x{ncol}")
        width_pct = _num(b.get("width", 100), "width") / 100
        total = int((self.text_w - 566) * min(max(width_pct, 0.1), 1.0))
        ratios = b.get("widths") or [1] * ncol
        if len(ratios) != ncol:
            raise SpecError(f"widths 는 열 수({ncol})만큼: {ratios}")
        rs = [_num(x, "widths") for x in ratios]
        colw = [int(total * x / sum(rs)) for x in rs]
        colw[-1] += total - sum(colw)

        spans: dict[tuple[int, int], tuple[int, int]] = {}
        covered: set[tuple[int, int]] = set()
        for m in b.get("merge") or []:
            if not (isinstance(m, list) and len(m) == 4):
                raise SpecError(f"merge 는 [행, 열, 행수, 열수]: {m}")
            r0, c0, rn, cn = (int(x) for x in m)
            if r0 < 0 or c0 < 0 or rn < 1 or cn < 1 or r0 + rn > nrow or c0 + cn > ncol:
                raise SpecError(f"merge 가 표를 벗어난다: {m} (표 {nrow}x{ncol})")
            spans[(r0, c0)] = (rn, cn)
            covered |= {(r, c) for r in range(r0, r0 + rn) for c in range(c0, c0 + cn)} - {(r0, c0)}

        head = int(b.get("header", 1))
        head_fill = _color(b.get("head_fill", "#D9D9D9")) if b.get("head_fill", "#D9D9D9") else None
        aligns = b.get("align") or []
        line_col = _color(b.get("line", "#000000"))
        mode = b.get("border", "grid")
        thick = "0.4 mm" if b.get("outer", "thick") == "thick" else "0.12 mm"
        row_h = int(_num(b.get("row_height", 7), "row_height") * MM)
        hs = [int(_num(x, "heights") * MM) for x in (b.get("heights") or [])]
        rh = [hs[r] if r < len(hs) else row_h for r in range(nrow)]       # 행별 높이 (없으면 row_height)
        base = {"size": b.get("size", 11), "font": b.get("font", "table"), "line": b.get("line_spacing", 130),
                "color": b.get("color")}
        fills = b.get("fill") or {}

        head_line = b.get("head_line")      # 머리행 아래 선: double | thick
        col_size = b.get("col_size") or []

        def bf(r, c, rn, cn, fill, cell=None):
            top = r == 0
            bot = r + rn == nrow
            lft = c == 0
            rgt = c + cn == ncol
            if mode == "none":
                sides = ("NONE",) * 4
            elif mode == "rows":       # 기준 문서처럼 양옆 세로선 없이 가로선만
                sides = ("NONE" if lft else "SOLID", "NONE" if rgt else "SOLID", "SOLID", "SOLID")
            else:
                sides = ("SOLID",) * 4
            w = ["0.12 mm"] * 4
            if mode != "none":
                if top:
                    w[2] = thick
                if bot:
                    w[3] = thick
                if mode == "grid":
                    if lft:
                        w[0] = thick
                    if rgt:
                        w[1] = thick
            if head_line and head and r + rn == head and mode != "none":
                sides = sides[:3] + ("DOUBLE_SLIM" if head_line == "double" else "SOLID",)
                w[3] = "0.5 mm" if head_line == "double" else thick
            sides = list(sides)
            for i, k in enumerate(("line_l", "line_r", "line_t", "line_b")):    # 칸 한 변만: "double"|"dash"|"none"…
                if cell and cell.get(k):
                    sides[i] = LINE.get(cell[k], "SOLID")
                    w[i] = "0.5 mm" if sides[i] == "DOUBLE_SLIM" else w[i]
            return self.refs.border_fill(*sides, fill=fill, width=tuple(w), color=line_col)

        pad = [int(_num(x, "pad") * MM) for x in (b.get("pad") or [1.8, 1.8, 0.5, 0.5])]
        trs = []
        for r in range(nrow):
            tcs = []
            for c in range(ncol):
                if (r, c) in covered:
                    continue
                raw = rows[r][c] if c < len(rows[r]) else ""
                cell = raw if isinstance(raw, dict) else {"t": "" if raw is None else str(raw)}
                rn, cn = spans.get((r, c), (1, 1))
                is_head = r < head
                fill = cell.get("fill") or fills.get(f"{r},{c}") or (head_fill if is_head else None)
                fill = _color(fill) if fill else None
                cs = {**base, "align": cell.get("align") or ("center" if is_head else
                                                             (aligns[c] if c < len(aligns) and aligns[c] else "left")),
                      "bold": cell.get("b", cell.get("bold", is_head)),
                      **({"size": col_size[c]} if c < len(col_size) and col_size[c] and not is_head else {}),
                      **{k: cell[k] for k in ("color", "size", "font", "shade", "italic", "underline") if k in cell}}
                body = cell.get("p", cell.get("t", ""))
                paras = body if isinstance(body, list) and body and all(isinstance(x, list) for x in body) else [body]
                inner = "".join(self._p(self._para(cs), self._runs(pp, cs)) for pp in paras)
                va = VALIGN.get(str(cell.get("valign", "center")).lower(), "CENTER")
                w = sum(colw[c:c + cn])
                tcs.append(
                    f'<hp:tc name="" header="{int(is_head)}" hasMargin="{int(bool(b.get("pad")))}" protect="0" editable="0" dirty="0" '
                    f'borderFillIDRef="{bf(r, c, rn, cn, fill, cell)}"><hp:subList id="" textDirection="HORIZONTAL" '
                    f'lineWrap="BREAK" vertAlign="{va}" linkListIDRef="0" linkListNextIDRef="0" textWidth="0" '
                    f'textHeight="0" hasTextRef="0" hasNumRef="0">{inner}</hp:subList>'
                    f'<hp:cellAddr colAddr="{c}" rowAddr="{r}"/><hp:cellSpan colSpan="{cn}" rowSpan="{rn}"/>'
                    f'<hp:cellSz width="{w}" height="{sum(rh[r:r + rn])}"/>'
                    f'<hp:cellMargin left="{pad[0]}" right="{pad[1]}" top="{pad[2]}" bottom="{pad[3]}"/></hp:tc>')
            trs.append(f"<hp:tr>{''.join(tcs)}</hp:tr>")
        return self._table_p(trs, nrow, ncol, total, sum(rh), b)  # b 의 page_break·before·after 도 쓴다

    def _table_p(self, trs: list[str], nrow: int, ncol: int, w: int, h: int, b: dict, in_margin=(510, 510, 141, 141)) -> str:
        self._pid += 1
        tid = self._pid
        al = ALIGN.get(str(b.get("table_align", "center")).lower(), "CENTER")
        tbl = (f'<hp:tbl id="{tid}" zOrder="0" numberingType="TABLE" textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" '
               f'lock="0" dropcapstyle="None" pageBreak="CELL" repeatHeader="1" rowCnt="{nrow}" colCnt="{ncol}" '
               f'cellSpacing="0" borderFillIDRef="1" noAdjust="0">'
               f'<hp:sz width="{w}" widthRelTo="ABSOLUTE" height="{h}" heightRelTo="ABSOLUTE" protect="0"/>'
               '<hp:pos treatAsChar="1" affectLSpacing="0" flowWithText="1" allowOverlap="0" holdAnchorAndSO="0" '
               'vertRelTo="PARA" horzRelTo="COLUMN" vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0"/>'
               '<hp:outMargin left="283" right="283" top="283" bottom="283"/>'
               f'<hp:inMargin left="{in_margin[0]}" right="{in_margin[1]}" top="{in_margin[2]}" bottom="{in_margin[3]}"/>'
               f'{"".join(trs)}</hp:tbl>')
        s = {"align": {"LEFT": "left", "RIGHT": "right"}.get(al, "center"), "before": b.get("before", 4),
             "after": b.get("after", 4), "line": 100}
        return self._p(self._para(s), f'<hp:run charPrIDRef="{self._char({"size": 4})}">{tbl}<hp:t/></hp:run>',
                       bool(b.get("page_break")))

    def rule(self, b: dict) -> str:
        color = _color(b.get("rule") if isinstance(b.get("rule"), str) else b.get("color", "#4472C4"))
        h = int(_num(b.get("thick", 2), "thick") * MM)
        fid = self.refs.border_fill(fill=color)
        cs = {"size": 1, "line": 100}
        cell = (f'<hp:tc name="" header="0" hasMargin="1" protect="0" editable="0" dirty="0" borderFillIDRef="{fid}">'
                '<hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK" vertAlign="CENTER" linkListIDRef="0" '
                'linkListNextIDRef="0" textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0">'
                f'{self._p(self._para(cs), self._runs("", cs))}</hp:subList>'
                '<hp:cellAddr colAddr="0" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/>'
                f'<hp:cellSz width="{self.text_w - 566}" height="{h}"/>'
                '<hp:cellMargin left="0" right="0" top="0" bottom="0"/></hp:tc>')
        return self._table_p([f"<hp:tr>{cell}</hp:tr>"], 1, 1, self.text_w - 566, h,
                             {"before": b.get("before", 0), "after": b.get("after", 0)}, (0, 0, 0, 0))

    def box(self, b: dict) -> str:
        body = b["box"]
        blocks = body if isinstance(body, list) and body and all(isinstance(x, dict) and
                                                                  ("p" in x or "style" in x) for x in body) else [{"p": body}]
        inner_style = {k: v for k, v in b.items() if k in ("style", "size", "bold", "color", "align", "font")}
        inner = "".join(self.paragraph({**inner_style, **x}) for x in blocks)
        border = LINE.get(b.get("border", "solid"), "SOLID")
        width = "0.5 mm" if border == "DOUBLE_SLIM" else "0.12 mm"
        fid = self.refs.border_fill(border, border, border, border, fill=_color(b["fill"]) if b.get("fill") else None,
                                    width=(width,) * 4, color=_color(b.get("line", "#000000")))
        w = int((self.text_w - 566) * _num(b.get("width", 100), "width") / 100)
        cell = (f'<hp:tc name="" header="0" hasMargin="1" protect="0" editable="0" dirty="0" borderFillIDRef="{fid}">'
                '<hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK" vertAlign="CENTER" linkListIDRef="0" '
                f'linkListNextIDRef="0" textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0">{inner}</hp:subList>'
                '<hp:cellAddr colAddr="0" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/>'
                f'<hp:cellSz width="{w}" height="{int(10 * MM)}"/>'
                '<hp:cellMargin left="1417" right="1417" top="850" bottom="850"/></hp:tc>')
        return self._table_p([f"<hp:tr>{cell}</hp:tr>"], 1, 1, w, int(10 * MM),
                             {"before": b.get("before", 6), "after": b.get("after", 6), "page_break": b.get("page_break")},
                             (1417, 1417, 850, 850))

    def toc(self, b: dict) -> str:
        tab = self.refs.tab_right_dot(self.text_w - int(_num(b.get("indent", 0), "indent") * MM) - 200)
        out = []
        for item in b["toc"]:
            if isinstance(item, str):
                item = [item]
            if not (isinstance(item, list) and 1 <= len(item) <= 2):
                raise SpecError(f"toc 항목은 [제목, 쪽] 또는 [제목] (쪽 자동): {item}")
            title = str(item[0])
            page = str(item[1]) if len(item) == 2 and str(item[1]) not in ("", "auto") else None
            if page is None:                    # 쪽 자동 — 한 번 그려 본 뒤 runner 가 채워 다시 만든다
                self.toc_auto.append(title)
                page = self.toc_pages.get(title, "00")
            lvl = 1 if title.startswith(("  ", "\u3000")) or re.match(r"^\s*\d+\.", title) else 0
            s = {"size": b.get("size", 13 if lvl else 15), "bold": b.get("bold", lvl == 0),
                 "font": "head" if lvl == 0 else None, "indent": 6 if lvl else 0, "before": 6 if lvl == 0 else 1}
            out.append(self._p(self._para(s, tab), self._runs(f"{title.strip()}\t{page}", s)))
        return "".join(out)

    # -- 머리말·쪽 번호 (첫 문단의 첫 런에 붙는다)
    def _hf_ctrls(self) -> str:
        out = []
        if self.page.get("header"):
            h = self.page["header"]
            h = h if isinstance(h, dict) else {"p": h}
            s = {"size": 10, "align": "right", "color": "#555555", "line": 130,
                 **{k: v for k, v in h.items() if k not in ("p", "t", "text")}}
            if s.pop("underline_bar", None) or h.get("rule"):
                s["line_below"] = h.get("rule") if isinstance(h.get("rule"), str) else "#1F3864"
            p = self._p(self._para(s), self._runs(h.get("p", h.get("t", h.get("text", ""))), s))
            out.append('<hp:ctrl><hp:header id="1" applyPageType="BOTH"><hp:subList id="" textDirection="HORIZONTAL" '
                       'lineWrap="BREAK" vertAlign="TOP" linkListIDRef="0" linkListNextIDRef="0" '
                       f'textWidth="{self.text_w}" textHeight="{int(12 * MM)}" hasTextRef="0" hasNumRef="0">{p}'
                       '</hp:subList></hp:header></hp:ctrl>')
        pn = self.page.get("page_number", "- n -")
        if pn:
            side = "-" if str(pn).strip().startswith("-") else ""
            out.append(f'<hp:ctrl><hp:pageNum pos="BOTTOM_CENTER" formatType="DIGIT" sideChar="{side}"/></hp:ctrl>')
        return "".join(out)

    def _cover(self) -> bool:
        return bool(self.page.get("cover", any(isinstance(x, dict) and x.get("rule") for x in self.spec["blocks"][:4])))

    def _sec_pr(self) -> str:
        t, bt, l, r = self.margin
        cover = int(self._cover())
        w, h = (PAGE_H, PAGE_W) if self.landscape else (PAGE_W, PAGE_H)
        return ('<hp:secPr id="" textDirection="HORIZONTAL" spaceColumns="0" tabStop="8000" tabStopVal="4000" '
                'tabStopUnit="HWPUNIT" outlineShapeIDRef="0" memoShapeIDRef="0" textVerticalWidthHead="0" masterPageCnt="0">'
                '<hp:grid lineGrid="0" charGrid="0" wonggojiFormat="0"/>'
                '<hp:startNum pageStartsOn="BOTH" page="0" pic="0" tbl="0" equation="0"/>'
                f'<hp:visibility hideFirstHeader="{cover}" hideFirstFooter="{cover}" hideFirstMasterPage="0" border="SHOW_ALL" '
                f'fill="SHOW_ALL" hideFirstPageNum="{cover}" hideFirstEmptyLine="0" showLineNumber="0"/>'
                '<hp:lineNumberShape restartType="0" countBy="0" distance="0" startNumber="0"/>'
                f'<hp:pagePr landscape="{"NARROWLY" if self.landscape else "WIDELY"}" width="{w}" height="{h}" gutterType="LEFT_ONLY">'
                f'<hp:margin header="{int(10 * MM)}" footer="{int(10 * MM)}" gutter="0" left="{l}" right="{r}" top="{t}" bottom="{bt}"/>'
                '</hp:pagePr><hp:footNotePr><hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar=")" supscript="0"/>'
                '<hp:noteLine length="-1" type="SOLID" width="0.12 mm" color="#000000"/>'
                '<hp:noteSpacing betweenNotes="283" belowLine="567" aboveLine="850"/>'
                '<hp:numbering type="CONTINUOUS" newNum="1"/><hp:placement place="EACH_COLUMN" beneathText="0"/></hp:footNotePr>'
                '<hp:endNotePr><hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar=")" supscript="0"/>'
                '<hp:noteLine length="14692344" type="SOLID" width="0.12 mm" color="#000000"/>'
                '<hp:noteSpacing betweenNotes="0" belowLine="567" aboveLine="850"/>'
                '<hp:numbering type="CONTINUOUS" newNum="1"/><hp:placement place="END_OF_DOCUMENT" beneathText="0"/></hp:endNotePr>'
                '<hp:pageBorderFill type="BOTH" borderFillIDRef="1" textBorder="PAPER" headerInside="0" footerInside="0" fillArea="PAPER">'
                '<hp:offset left="1417" right="1417" top="1417" bottom="1417"/></hp:pageBorderFill></hp:secPr>'
                '<hp:ctrl><hp:colPr id="" type="NEWSPAPER" layout="LEFT" colCount="1" sameSz="1" sameGap="0"/></hp:ctrl>')

    # -- 전체
    def _block(self, b: Any) -> str:
        if isinstance(b, str):
            b = {"p": b}
        if not isinstance(b, dict):
            raise SpecError(f"블록은 객체여야 한다: {b!r}"[:200])
        t = b.get("type")             # 예전 scaffold 형식
        if t == "heading":
            b = {**b, "p": b.get("text", ""), "style": b.get("style") or f"h{min(int(b.get('level', 1)), 3)}"}
        elif t == "paragraph":
            b = {**b, "p": b.get("text", "")}
        elif t == "table":
            b = {**b, "table": b.get("rows", [])}
        if "table" in b:
            return self.table(b)
        if "rule" in b:
            return self.rule(b)
        if "box" in b:
            return self.box(b)
        if "toc" in b:
            return self.toc(b)
        if "space" in b:
            s = {"size": max(_num(b["space"], "space"), 1), "line": 100}
            return self._p(self._para(s), self._runs("", s))
        if set(b) == {"page_break"}:
            s = {"size": 1, "line": 100}
            return self._p(self._para(s), self._runs("", s), page_break=True)
        if any(k in b for k in ("p", "t", "text")):
            return self.paragraph(b)
        raise SpecError(f"블록 종류를 모르겠다 (p·table·rule·box·toc·space·page_break): {sorted(b)}")

    def build(self, skeleton: bytes) -> bytes:
        blocks = self.spec.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise SpecError("blocks 가 비었다.")
        if len(blocks) > 3000:
            raise SpecError("블록이 너무 많다 (3000).")
        title = self.spec.get("title")
        if title and not any(isinstance(x, dict) and x.get("style") == "title" for x in blocks):
            blocks = [{"p": title, "style": "title"}] + blocks
        body = [self._block(b) for b in blocks]
        # 첫 문단 맨 앞에 구역 정의·머리말·쪽 번호
        first = body[0]
        hide = ""
        if self._cover():
            hide = ('<hp:ctrl><hp:pageHiding hideHeader="1" hideFooter="1" hideMasterPage="0" hideBorder="0" '
                    'hideFill="0" hidePageNum="1"/></hp:ctrl>')
        lead = f'<hp:run charPrIDRef="0">{self._sec_pr()}{self._hf_ctrls()}{hide}</hp:run>'
        i = first.index(">") + 1
        body[0] = first[:i] + lead + first[i:]
        return self._pack(skeleton, "".join(body))

    def _pack(self, skeleton: bytes, body: str) -> bytes:
        zin = zipfile.ZipFile(io.BytesIO(skeleton))
        header = zin.read("Contents/header.xml").decode("utf-8")
        section = zin.read("Contents/section0.xml").decode("utf-8")
        header = re.sub(r"<hh:fontfaces.*?</hh:paraProperties>", lambda _m: self.refs.xml(), header, count=1, flags=re.S)
        if "<hh:fontfaces" not in header:
            raise SpecError("뼈대 header.xml 형식이 예상과 다르다.")
        head_open = section[: section.index("<hp:p ")]
        section = head_open + body + "</hs:sec>"
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename == "Contents/header.xml":
                    data = header.encode("utf-8")
                elif info.filename == "Contents/section0.xml":
                    data = section.encode("utf-8")
                elif info.filename == "Preview/PrvText.txt":
                    data = re.sub(r"<[^>]+>", "", body)[:1000].encode("utf-8")
                z.writestr(info, data, compress_type=zipfile.ZIP_STORED if info.filename == "mimetype"
                           else zipfile.ZIP_DEFLATED)
        return out.getvalue()


def build(spec: dict, skeleton: bytes, toc_pages: dict[str, str] | None = None) -> tuple[bytes, Builder]:
    b = Builder(spec)
    b.toc_pages = toc_pages or {}
    return b.build(skeleton), b


def _norm(s: str) -> str:
    return re.sub(r"[\s.·:]", "", s)


def toc_pages_from_text(pages: list[str], titles: list[str], restart: tuple[str, int] | None) -> dict[str, str]:
    """쪽별 본문(export-text)에서 목차 제목이 실제로 나오는 쪽을 찾는다. 목차 자체(제목+"00")가 있는 쪽 뒤에서만
    찾고, 쪽 번호를 새로 시작한 문단이 있으면 그 쪽을 시작 번호로 센다."""
    np_ = [_norm(p) for p in pages]
    toc_last = max((i for i, p in enumerate(np_) for t in titles if _norm(t) + "00" in p), default=-1)
    offset = 1                                  # 표시 쪽 = 쪽 순번 + offset
    if restart and _norm(restart[0]):
        for i in range(toc_last + 1, len(np_)):
            if _norm(restart[0]) in np_[i]:
                offset = restart[1] - i
                break
    out: dict[str, str] = {}
    for t in titles:
        nt = _norm(t)
        for i in range(toc_last + 1, len(np_)):
            if nt and nt in np_[i]:
                out[t] = str(i + offset)
                break
        else:
            out[t] = "-"
    return out
