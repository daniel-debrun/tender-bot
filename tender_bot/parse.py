"""Rule-based extraction from SC TO-I originals and SC TO-I/A amendments (checked against the hand-verified
filings in tests/fixtures)."""
import re
from datetime import datetime

MON = r'(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sept?\.?|Oct\.?|Nov\.?|Dec\.?)'
DATE = rf'({MON}\s+\d{{1,2}}\s*,\s*\d{{4}})'
NUM = r'(?:U\.?S\.?\s?)?\$\s?(\d{1,4}(?:,\d{3})*(?:\.\d+)?)'
I = re.I | re.S


def num(s):
    try:
        return float(s.replace(',', ''))
    except Exception:
        return None


def pdate(s):
    s = re.sub(r'\s+', ' ', s.replace('.', '')).replace(' ,', ',')
    for f in ('%B %d, %Y', '%b %d, %Y'):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    try:
        return datetime.strptime(s.replace('Sept', 'Sep'), '%b %d, %Y').date()
    except ValueError:
        return None


def price_in(x):
    """First '$X per share' in x that is not a par/nominal value."""
    for m in re.finditer(rf'{NUM}\s+(?:net\s+)?per\s+(?:share|unit)', x, re.I):
        if not re.search(r'(?:par|nominal)\s+value\s*(?:of\s*)?$', x[max(0, m.start() - 25):m.start()], re.I):
            return num(m.group(1))
    m = re.search(rf'price\s+per\s+(?:share|unit)\s+(?:of|was|is|equal\s+to)\s+{NUM}', x, re.I)
    return num(m.group(1)) if m else None


def cover(main):
    """Title of class and CUSIP from the Schedule TO cover page."""
    m = re.search(r'\(?\s*Titles?\s+(?:of|and)\s+Class(?:es)?\s+of\s+Securities?\s*\)?', main, re.I)
    if not m:
        return None, None
    pre = main[max(0, m.start() - 600):m.start()]
    parts = re.split(r'\((?:Name|Names)\s+of[^)]*\)|Identifying Status[^)\n]*\)?|\(Issuer\)|\(Offeror\)', pre, flags=re.I)
    title = re.sub(r'[\s\-_=*]+', ' ', parts[-1]).strip()[-300:]
    post = main[m.end():m.end() + 400]
    c = re.search(r'\(?\s*CUSIP\s+Numbers?', post, re.I)
    seg = post[:c.start()] if c else post[:120]
    cus = [re.sub(r'[\s\-]', '', x).upper() for x in re.findall(r'\b[0-9A-Z]{6}[\s\-]?[0-9A-Z]{2}[\s\-]?[0-9]?\b', seg)]
    cus = [x for x in cus if re.search(r'\d', x) and len(x) >= 8]
    return title, (cus[0][:8] if cus else None)


def sec_class(title):
    t = (title or '').lower()
    if not t:
        return 'unknown'
    if 'option' in t:
        return 'options'
    if 'warrant' in t:
        return 'warrants'
    if re.search(r'\bnotes?\b|debenture|bonds?\b|\bdebt\b', t):
        return 'debt'
    if 'warrant' in t and 'common' not in t:
        return 'warrants'
    if re.search(r'preferred|preference', t) and not re.search(r'common', t):
        return 'preferred'
    if re.search(r'equity\s+security\s+units|corporate\s+units|income\s+units|purchase\s+contract|\bpies\b|\bfeline\b', t):
        return 'other'
    if re.search(r'common|ordinary|class [ab] |shares|stock|beneficial interest|units?\b', t):
        return 'common'
    return 'other'


def window(t, pat, w=250, flags=I):
    m = re.search(pat, t, flags)
    return t[max(0, m.start() - w):m.end() + w] if m else ''


def parse_original(D):
    main = D[0]['text'] if D else ''
    title, cusip = cover(main)
    # offer to purchase: the largest document mentioning "offer to purchase" (else the largest document)
    cand = [d for d in D if re.search(r'offer\s+to\s+purchase', d['text'][:20000], re.I)] or D
    ofp = max(cand, key=lambda d: len(d['text']))['text'] if cand else ''
    head = ofp[:40000]
    allt = '\n'.join(d['text'] for d in D)[:2_000_000]
    r = dict(title=title, cusip=cusip, sclass=sec_class(title))
    r['exchange'] = bool(re.search(r'offer\s+to\s+exchange', main[:20000] + head[:5000], re.I)) and not re.search(r'offer\s+to\s+purchase', head[:5000], re.I)
    lo = hi = fixed = None
    PS = r'(?:\s+per\s+share)?,?'
    m = (re.search(rf'not\s+(?:greater|more)\s+than\s+{NUM}{PS}\s+(?:nor|or|and)\s+(?:not\s+)?less\s+than\s+{NUM}', head, I)
         or re.search(rf'not\s+less\s+than\s+{NUM}{PS}\s+(?:nor|or|and)\s+(?:not\s+)?(?:greater|more)\s+than\s+{NUM}', head, I)
         or re.search(rf'not\s+(?:greater|more)\s+than\s+{NUM}{PS}\s+(?:nor|or|and)\s+(?:not\s+)?less\s+than\s+{NUM}', main[:20000], I)
         or re.search(rf'not\s+less\s+than\s+{NUM}{PS}\s+(?:nor|or|and)\s+(?:not\s+)?(?:greater|more)\s+than\s+{NUM}', main[:20000], I)
         or re.search(rf'(?:price|prices)\s+(?:range\s+)?(?:of\s+|between\s+|from\s+){NUM}\s+(?:to|and)\s+{NUM}\s+per\s+share', head, I))
    if m:
        a, b = num(m.group(1)), num(m.group(2))
        if a and b:
            lo, hi = min(a, b), max(a, b)
    dutch = lo is not None and hi is not None and hi > lo
    if not dutch:
        m = (re.search(rf'(?:at\s+a\s+)?(?:purchase\s+)?price\s+of\s+{NUM}\s+per\s+(?:share|unit)', head, I)
             or re.search(rf'{NUM}\s+per\s+(?:share|unit),?\s+net\s+to\s+the\s+seller', head, I)
             or re.search(rf'for\s+{NUM}\s+per\s+(?:share|unit)', head, I))
        if m:
            fixed = num(m.group(1))
        else:
            m = re.search(rf'(?:at|of)\s+{NUM}\s+(?:net\s+)?per\s+(?:share|unit)', main[:15000], I)
            if m and not re.search(r'par\s+value\s*$', main[max(0, m.start() - 30):m.start()], re.I):
                fixed = num(m.group(1))
                r['fixed_from_fee'] = True
    r.update(dutch=dutch, lo=lo, hi=hi, fixed=fixed,
             dutch_word=bool(re.search(r'dutch\s+auction', head, re.I)))
    m = re.search(r'(?:up\s+to|purchase)\s+(?:an\s+aggregate\s+of\s+)?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?\s+million)\s+(?:of\s+(?:its|our|the)\s+)?(?:outstanding\s+)?(?:shares|common|ordinary|units)', head, I)
    if m:
        g = m.group(1)
        r['sought'] = num(g.split()[0]) * 1e6 if 'million' in g else num(g)
    else:
        r['sought'] = None
    m = re.search(r'up\s+to\s+\$\s?(\d+(?:\.\d+)?)\s+(million|billion)\s+(?:in\s+(?:aggregate\s+)?(?:value|purchase\s+price)\s+of|of\s+(?:its|our|the))', head, I)
    r['sought_usd'] = num(m.group(1)) * (1e9 if m.group(2).lower() == 'billion' else 1e6) if m else None
    r['oddlot'] = bool(re.search(r'odd[\s\-]+lot|(?:fewer|less)\s+than\s+(?:100|one\s+hundred)\s+(?:\(100\)\s+)?shares', allt, re.I))
    r['oddlot_priority'] = bool(re.search(r'(?:odd[\s\-]+lot|(?:fewer|less)\s+than\s+(?:100|one\s+hundred)\s+(?:\(100\)\s+)?shares)[^.]{0,400}(?:priority|preference|without\s+proration|not\s+be\s+subject\s+to\s+proration|first)', allt, I)
                                or re.search(r'(?:priority|preference|first)[^.]{0,200}(?:odd[\s\-]+lot|(?:fewer|less)\s+than\s+(?:100|one\s+hundred)\s+shares)', allt, I))
    r['any_all'] = bool(re.search(r'any\s+and\s+all', head[:8000], re.I))
    r['prorat_word'] = bool(re.search(r'prorat', allt, re.I))
    hh = main[:20000] + ' ' + head[:8000]
    r['oddlot_only'] = bool(re.search(r'(?:99|ninety-nine)\s+(?:\(99\)\s+)?or\s+fewer\s+shares', hh, re.I)
                            and re.search(r'odd[\s\-]+lot\s+(?:tender\s+)?offer|all\s+(?:of\s+the\s+)?(?:outstanding\s+)?(?:shares|common)[^.]{0,120}(?:99|ninety-nine)', hh, re.I))
    if r['oddlot_only']:
        r['oddlot_priority'] = True
    ex = None
    for m in re.finditer(r'expir\w*', head[:60000], re.I):
        d = re.search(DATE, head[m.end():m.end() + 250], re.I)
        if d and (ex := pdate(d.group(1))):
            break
    r['expire'] = ex
    return r


def parse_amend(D):
    t = '\n'.join(d['text'] for d in D)[:600_000]
    r = {}
    sents = re.split(r'(?<=[a-z0-9\)\u201d"])\.\s+(?=[A-Z\u201c"(])', re.sub(r'\s+', ' ', t))
    TERM = re.compile(r'\b(?:has|have|had)\s+(?:been\s+)?(?:terminated|withdrawn)\b|\bterminated\s+(?:the|its|our)\s+(?:\w+\s+){0,3}(?:tender\s+|exchange\s+)?offer|'
                      r'announc\w+\s+(?:the\s+|its\s+)?(?:decision\s+to\s+)?terminat\w*|\b(?:is|are)\s+(?:hereby\s+)?(?:terminating|withdrawing)\s+(?:the|its|our)\s+(?:\w+\s+)?offer|'
                      r'(?:tender\s+|exchange\s+)?offer\s+(?:was|has\s+been|is\s+hereby|is)\s+(?:terminated|withdrawn)', re.I)
    r['terminated'] = any(TERM.search(x) and re.search(r'offer', x, re.I)
                          and not re.search(r'withdrawal\s+rights|may\s+be\s+withdrawn|validly\s+withdrawn|properly\s+withdrawn|tendered\s+and\s+not|\bif\b|\bmay\b|\bwill\s+not\b|right\s+to|\bunless\b|\bwould\b', x)
                          for x in sents if len(x) < 1500)
    act = re.compile(r'accepted\s+for\s+(?:purchase|payment)|\b(?:is|are)\s+accepting\b|\b(?:to\s+be|will\s+be|were|was|have\s+been)\s+(?:purchased|accepted)\b|(?:has|have|had|will)\s+(?:purchased?|repurchased?|acquired?|accept(?:ed)?)\b|\bpurchased\s+(?:an\s+aggregate|a\s+total)|\bexpects?\s+to\s+(?:purchase|accept|acquire)', re.I)
    fp = sp = None
    hit = False
    for x in sents:
        ma = act.search(x)
        if len(x) > 1500 or not ma or (re.search(r'\bup\s+to\b|\bassum|not\s+(?:greater|more|less)\s+than|\brange\b', x, re.I) or re.search(r'\bmay\b|\bif\b|\bwould\b|\bIf\b', x)):
            continue
        xa = x[ma.start():]
        m_s = re.search(r'(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?\s+million)\s+(?:of\s+(?:its|the|our)\s+)?(?:outstanding\s+|issued\s+|tendered\s+)?(?:shares|common|ordinary|units)', xa, re.I)
        p_ = price_in(xa)
        if m_s and p_:
            g = m_s.group(1)
            sp = num(g.split()[0]) * 1e6 if 'million' in g else num(g)
            fp = p_
            hit = True
    if fp is None and (hit or re.search(r'(?:final|preliminary)\s+(?:count|results)', t, re.I)):
        ms = list(re.finditer(rf'(?:tendered|withdrawn)[^.]{{0,120}}?at\s+(?:or\s+below\s+)?(?:(?:a|the)\s+(?:final\s+)?(?:purchase\s+)?price\s+of\s+)?{NUM}(?:\s+per\s+share)?', t, I))
        if ms:
            fp = num(ms[-1].group(1))
    if fp is None:
        m = re.search(rf'final\s+purchase\s+price\s+(?:of|was|is|will\s+be)\s+{NUM}', t, I)
        if m:
            fp = num(m.group(1))
    r['results'] = hit or bool(re.search(r'(?:announc|report)\w*\s+(?:the\s+)?(?:final|preliminary)\s+(?:results|count)', t, re.I))
    r['results_strict'] = hit
    r['final_amend'] = bool(re.search(r'final\s+amendment', t, re.I))
    r['final_price'], r['purchased'] = fp, sp
    m = re.search(r'(\d{1,3}(?:,\d{3})+)\s+(?:shares|Shares)(?:\s+of\s+(?:its\s+|our\s+|the\s+Company.s\s+)?(?:common\s+stock|Common\s+Stock|common\s+shares))?(?:,[^.]{0,100}?,)?\s+(?:were|had\s+been|have\s+been)\s+(?:properly|validly)\s+tendered', ' '.join(x for x in sents if re.search(r'final|preliminary|count|depositary', x, re.I))[:200000], re.I)
    r['tendered'] = num(m.group(1)) if m else None
    pr = None
    m = re.search(r'proration\s+factor[^.%]{0,130}?(\d{1,3}(?:\.\d+)?)\s*%', t, re.I)
    if m:
        pr = num(m.group(1)) / 100
    else:
        m = re.search(r'proration\s+factor[^.]{0,60}?\b(0?\.\d+|1\.0+)\b', t, re.I)
        if m:
            pr = num(m.group(1))
    r['proration'] = pr if pr is not None and 0 < pr <= 1 else None
    r['undersub'] = bool(re.search(r'undersubscribed|no\s+proration|not\s+(?:be\s+)?subject\s+to\s+proration|all\s+(?:of\s+the\s+)?shares\s+(?:that\s+were\s+)?(?:properly|validly)\s+tendered[^.]{0,120}(?:will\s+be|were|have\s+been)\s+(?:accepted|purchased)', t, I))
    r['oversub'] = bool(re.search(r'oversubscri', t, re.I))
    ex = None
    for m in re.finditer(r'extend\w*[^.]{0,250}?expir\w*|expir\w*[^.]{0,80}?extended', t, I):
        d = re.search(DATE, t[m.start():m.end() + 300], re.I)
        if d:
            ex = pdate(d.group(1))
    r['new_expire'] = ex
    m = re.search(rf'expired\s+(?:at\s+)?[^.]{{0,90}}?\bon\s+(?:\w+day,?\s+)?{DATE}', t, I)
    r['expired_on'] = pdate(m.group(1)) if m else None
    m = re.search(rf'(?:increas|amend)\w*[^.]{{0,150}}(?:purchase\s+)?price[^.]{{0,60}}?to\s+{NUM}\s+per\s+share', t, I)
    r['price_change'] = num(m.group(1)) if m else None
    nl = nh = None
    for m in re.finditer(rf'(?:amend|increas|chang|revis)\w*[^.]{{0,200}}?not\s+(?:greater|more)\s+than\s+{NUM}(?:\s+per\s+share)?,?\s+(?:nor|or|and)\s+(?:not\s+)?less\s+than\s+{NUM}|(?:amend|increas|chang|revis)\w*[^.]{{0,200}}?not\s+less\s+than\s+{NUM}(?:\s+per\s+share)?,?\s+(?:nor|or|and)\s+(?:not\s+)?(?:greater|more)\s+than\s+{NUM}', t, I):
        g = [num(x) for x in m.groups() if x]
        if len(g) == 2:
            nl, nh = min(g), max(g)
    r['new_lo'], r['new_hi'] = nl, nh
    return r
