"""Render an :class:`ArticleDraft` to a standalone LaTeX article.

Deliberately minimal and self-contained: ``article`` class, an abstract, the
sections as ``\\section``s, and a hand-rolled ``thebibliography``. No
external ``.bib`` so the file compiles with a bare ``pdflatex`` run.
"""

from __future__ import annotations

from cophilo.draft.schemas import ArticleDraft

_TEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def tex_escape(text: str) -> str:
    """Escape LaTeX specials. Backslash is mapped first via a sentinel so the
    braces it introduces aren't re-escaped."""
    out = text.replace("\\", "\x00")
    for ch, repl in _TEX_SPECIALS.items():
        if ch == "\\":
            continue
        out = out.replace(ch, repl)
    return out.replace("\x00", _TEX_SPECIALS["\\"])


def _paragraphs(body: str) -> str:
    blocks = [b.strip() for b in body.split("\n\n") if b.strip()]
    return "\n\n".join(tex_escape(b) for b in blocks)


def render_tex(draft: ArticleDraft, *, language: str = "en") -> str:
    babel = "french" if language == "fr" else "english"
    lines: list[str] = [
        r"\documentclass[11pt,a4paper]{article}",
        r"\usepackage[utf8]{inputenc}",
        r"\usepackage[T1]{fontenc}",
        rf"\usepackage[{babel}]{{babel}}",
        r"\usepackage{microtype}",
        r"\title{" + tex_escape(draft.title) + "}",
        r"\author{}",
        r"\date{\today}",
        r"\begin{document}",
        r"\maketitle",
        "",
        r"\begin{abstract}",
        tex_escape(draft.abstract.strip()),
        r"\end{abstract}",
        "",
    ]
    if draft.keywords:
        kw = ", ".join(tex_escape(k) for k in draft.keywords)
        lines += [r"\noindent\textbf{Keywords:} " + kw + ".", ""]

    for s in draft.sections:
        lines.append(r"\section{" + tex_escape(s.heading) + "}")
        lines.append(_paragraphs(s.body))
        lines.append("")

    if draft.references:
        lines.append(r"\begin{thebibliography}{99}")
        for ref in draft.references:
            lines.append(r"\bibitem{} " + tex_escape(ref.strip()))
        lines.append(r"\end{thebibliography}")
        lines.append("")

    lines.append(r"\end{document}")
    return "\n".join(lines) + "\n"
