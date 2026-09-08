# -*- coding: utf-8 -*-
"""
report.py

분석 결과를 한 장짜리 리포트로 만들어 PDF로 저장하거나 프린터로 인쇄한다.

화면에서 본 것(입력 파일·물성치·추정 결과·그래프)을 그대로 종이에 옮기는 것이
목적이라, 그래프는 다시 그리지 않고 화면에 떠 있는 matplotlib Figure를 그대로
이미지로 떠서 넣는다. QTextDocument는 data: URI를 읽지 못하므로 이미지는
addResource()로 문서에 직접 붙인다.
"""

from __future__ import annotations

import io
import os
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QSizeF, QUrl
from PySide6.QtGui import QImage, QPageSize, QTextDocument
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import QFileDialog, QMessageBox

PLOT_URL = "report://plot.png"

STYLE = """
<style>
  body { font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif; font-size: 10pt; }
  h1 { font-size: 15pt; margin: 0 0 2px 0; }
  h2 { font-size: 11pt; margin: 14px 0 4px 0; }
  .meta { color: #555555; font-size: 8.5pt; margin: 0 0 10px 0; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #BBBBBB; padding: 3px 6px; font-size: 9pt; }
  th { background-color: #EFF2E8; text-align: left; }
  td.num { text-align: right; }
  .note { color: #555555; font-size: 8.5pt; margin-top: 4px; }
</style>
"""


def _escape(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def table_html(headers: Sequence[str], rows: Sequence[Sequence], numeric_from: int = 99) -> str:
    """헤더 한 줄 + 데이터 여러 줄짜리 표. numeric_from 이후 열은 오른쪽 정렬한다."""
    head = "".join(f"<th>{_escape(h)}</th>" for h in headers)
    body = []
    for row in rows:
        cells = "".join(
            f'<td class="num">{_escape(v)}</td>' if i >= numeric_from else f"<td>{_escape(v)}</td>"
            for i, v in enumerate(row)
        )
        body.append(f"<tr>{cells}</tr>")
    return f"<table><tr>{head}</tr>{''.join(body)}</table>"


def kv_table_html(rows: Sequence[Tuple[str, str]]) -> str:
    """항목/값 두 칸짜리 표."""
    body = "".join(
        f"<tr><th width='28%'>{_escape(k)}</th><td>{_escape(v)}</td></tr>" for k, v in rows
    )
    return f"<table>{body}</table>"


def build_html(title: str, subtitle: str, blocks: Sequence[Tuple[str, str]],
               plot_width: int = 560, plot_height: int = 420,
               with_plot: bool = True, footer: str = "") -> str:
    """(소제목, HTML) 목록을 받아 리포트를 만든다.

    plot_width/plot_height는 인쇄 시점에 실제 인쇄 영역과 그림 비율에서 계산해 넣는다.
    고정값으로 두면 종이 밖으로 밀리고, 높이를 빼먹으면 QTextDocument가 원본 픽셀
    높이를 그대로 써서 그림이 쪽 경계에서 잘린다.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [STYLE, f"<h1>{_escape(title)}</h1>",
             f'<p class="meta">{_escape(subtitle)} · 작성 {stamp}</p>']
    for heading, html in blocks:
        if heading:
            parts.append(f"<h2>{_escape(heading)}</h2>")
        parts.append(html)
    # 각주는 그래프보다 앞에 둔다. 그래프 뒤에 두면 마지막 쪽에 한 줄만 남는다.
    if footer:
        parts.append(f'<p class="note">{_escape(footer)}</p>')
    if with_plot:
        # 그래프는 새 쪽에서 시작하게 한다. 표 뒤에 그대로 이어 붙이면 이미지가
        # 쪽 경계에서 잘린 채로 인쇄된다.
        parts.append('<h2 style="page-break-before:always">크로마토그램</h2>')
        parts.append(f'<p><img src="{PLOT_URL}" width="{plot_width}" height="{plot_height}"></p>')
    return "".join(parts)


def figure_to_image(figure, dpi: int = 150) -> Optional[QImage]:
    """화면에 그려진 matplotlib Figure를 그대로 QImage로 뜬다."""
    if figure is None:
        return None
    buf = io.BytesIO()
    figure.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor="white")
    image = QImage()
    image.loadFromData(buf.getvalue(), "PNG")
    return image if not image.isNull() else None


def _document_for(printer: QPrinter, html_builder, plot_image: Optional[QImage]) -> QTextDocument:
    """프린터의 실제 인쇄 영역을 재서 그 폭에 맞춘 문서를 만든다.

    문서 쪽 크기와 종이의 인쇄 영역이 어긋나면 표나 그림이 쪽 경계에서 잘리므로,
    둘을 같은 값으로 맞춰야 한다.
    """
    rect = printer.pageLayout().paintRectPixels(printer.resolution())
    doc = QTextDocument()
    width = int(rect.width() * 0.98)
    height = int(width * 0.75)
    if plot_image is not None:
        doc.addResource(QTextDocument.ImageResource, QUrl(PLOT_URL), plot_image)
        if plot_image.width():
            height = int(width * plot_image.height() / plot_image.width())
        # 그림 하나가 한 쪽을 넘지 않도록 (제목 줄 몫을 조금 남긴다)
        max_height = int(rect.height() * 0.92)
        if height > max_height:
            width = int(width * max_height / height)
            height = max_height
    doc.setHtml(html_builder(width, height))
    doc.setPageSize(QSizeF(rect.width(), rect.height()))
    return doc


def save_pdf(parent, html_builder, plot_image: Optional[QImage], default_name: str) -> Optional[str]:
    """PDF로 저장. 저장한 경로를 돌려주고, 취소하면 None."""
    path, _ = QFileDialog.getSaveFileName(
        parent, "리포트를 PDF로 저장", default_name, "PDF 파일 (*.pdf)")
    if not path:
        return None
    if not path.lower().endswith(".pdf"):
        path += ".pdf"

    # HighResolution(1200dpi)을 쓰면 px 단위로 지정한 이미지 폭이 종이에서 손톱만
    # 하게 나온다. ScreenResolution(96dpi)이라야 문서에 적은 pt/px가 화면과 같은
    # 비율로 인쇄되고, PDF의 글자는 그대로 벡터로 들어간다.
    printer = QPrinter(QPrinter.ScreenResolution)
    printer.setOutputFormat(QPrinter.PdfFormat)
    printer.setPageSize(QPageSize(QPageSize.A4))
    printer.setOutputFileName(path)
    try:
        _document_for(printer, html_builder, plot_image).print_(printer)
    except Exception as e:  # noqa: BLE001
        QMessageBox.critical(parent, "저장 실패", f"PDF를 저장하지 못했습니다:\n{e}")
        return None
    return path


def print_document(parent, html_builder, plot_image: Optional[QImage]) -> bool:
    """프린터로 인쇄. 인쇄 대화상자에서 취소하면 False."""
    printer = QPrinter(QPrinter.ScreenResolution)
    printer.setPageSize(QPageSize(QPageSize.A4))
    dialog = QPrintDialog(printer, parent)
    dialog.setWindowTitle("리포트 인쇄")
    if dialog.exec() != QPrintDialog.Accepted:
        return False
    _document_for(printer, html_builder, plot_image).print_(printer)
    return True


def default_filename(prefix: str, sample_hint: str = "") -> str:
    """바탕화면 대신 마지막 작업 폴더를 강요하지 않도록 파일명만 만들어 준다."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    hint = os.path.splitext(os.path.basename(sample_hint))[0] if sample_hint else ""
    parts = [p for p in (prefix, hint, stamp) if p]
    return "_".join(parts) + ".pdf"
