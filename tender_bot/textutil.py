"""Text helpers for SEC filings."""
import gzip, html, re

_DOC = re.compile(r'<DOCUMENT>(.*?)</DOCUMENT>', re.S)
_TAG = re.compile(r'<[^>]+>')


def load(path):
    return gzip.decompress(open(path, 'rb').read()).decode('latin-1', errors='replace')


def header(raw):
    h = raw.split('</SEC-HEADER>', 1)[0]
    g = lambda k: (m.group(1).strip() if (m := re.search(k + r':\s*(.+)', h)) else None)
    subj = h.split('SUBJECT COMPANY:', 1)[-1]
    sc = re.search(r'CENTRAL INDEX KEY:\s*(\d+)', subj)
    sn = re.search(r'COMPANY CONFORMED NAME:\s*(.+)', subj)
    sic = re.search(r'STANDARD INDUSTRIAL CLASSIFICATION:\s*(.+)', subj)
    return dict(form=g('CONFORMED SUBMISSION TYPE'), filed=g('FILED AS OF DATE'),
                accepted=g('<ACCEPTANCE-DATETIME>') if '<ACCEPTANCE-DATETIME>' in h else None,
                subj_cik=int(sc.group(1)) if sc else None, subj_name=sn.group(1).strip() if sn else None,
                sic=sic.group(1).strip() if sic else None)


def clean(t):
    t = re.sub(r'(?is)<(script|style).*?</\1>', ' ', t)
    t = re.sub(r'(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>', '\n', t)
    t = _TAG.sub(' ', t)
    t = html.unescape(t).replace('\xa0', ' ')
    t = re.sub(r'[ \t\r\f\v]+', ' ', t)
    t = re.sub(r'\n\s*\n+', '\n', t)
    return t


def docs(raw):
    out = []
    for d in _DOC.findall(raw):
        ty = re.search(r'<TYPE>([^\n<]+)', d)
        de = re.search(r'<DESCRIPTION>([^\n<]+)', d)
        body = d.split('<TEXT>', 1)[-1]
        if re.search(r'(?i)^\s*(begin \d{3} |<PDF>)', body[:200]) or (ty and ty.group(1).strip().upper() in ('GRAPHIC', 'ZIP', 'EXCEL', 'PDF', 'XML', 'JSON')):
            continue
        out.append(dict(type=(ty.group(1).strip() if ty else ''), desc=(de.group(1).strip() if de else ''), text=clean(body)))
    return out
