# -*- coding: utf-8 -*-
"""Offline tests for translate.py v2.1.0 optimizations."""
import os, sys, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
from translate import (freeze_tokens, thaw_tokens, clean_thai_output,
                       _validate_line, _learn_speaker, _translate_batch,
                       detect_speaker, TerminologyDB, CharacterMemory)

ok = True
def check(name, cond):
    global ok
    print(('PASS' if cond else 'FAIL'), name)
    if not cond: ok = False

# 1. freeze/thaw round trip on the real failing line
en = 'MC first name: {color=[KoGa3Color2]}[mc_name]{/color}'
fz, toks = freeze_tokens(en)
check('freeze: tags replaced', fz == 'MC first name: ⟦1⟧⟦2⟧⟦3⟧')
check('freeze: 3 tokens', toks == ['{color=[KoGa3Color2]}', '[mc_name]', '{/color}'])
model_out = 'ชื่อจริงของตัวเอก: ⟦1⟧⟦2⟧⟦3⟧'
th = thaw_tokens(clean_thai_output(model_out, fz), toks)
check('thaw: tags restored', th == 'ชื่อจริงของตัวเอก: {color=[KoGa3Color2]}[mc_name]{/color}')
check('validate: thawed passes', _validate_line(en, th))

# 2. speaker labels NOT frozen (must stay transliteratable)
fz2, toks2 = freeze_tokens('[Anna]')
check('speaker label unfrozen', fz2 == '[Anna]' and toks2 == [])

# 3. cleaner preserves emoji now
emoji = 'รูปสวยมาก ⚜️ ความทรงจำสุดวิเศษ \U0001f4ab\U0001f9ff\U0001f54a️'
check('clean: emoji preserved', clean_thai_output(emoji, 'Great photos') == emoji)

# 4. cleaner strips CJK leakage
check('clean: CJK stripped', '你好' not in clean_thai_output('สวัสดี你好ครับ', 'Hello'))
check('clean: kana stripped', 'こんにちは' not in clean_thai_output('สวัสดีこんにちはครับ', 'Hello'))

# 5. speaker transliteration learning
db = TerminologyDB('')
_learn_speaker(db, '[Anna]', '[แอนนา]')
_learn_speaker(db, 'Bob:', 'บ็อบ:')
check('learn: [Anna] -> แอนนา', db._terms.get('Anna') == 'แอนนา')
check('learn: Bob: -> บ็อบ', db._terms.get('Bob') == 'บ็อบ')
_learn_speaker(db, 'Plain dialogue line here', 'ข้อความ')
check('learn: non-speaker ignored', len(db._terms) == 2)

# 6. fake-session integration: retry only failed lines, English fallback
class FakeResp:
    def __init__(self, content, finish='stop'):
        self.status_code = 200
        self._c, self._f = content, finish
    def json(self):
        return {'choices': [{'message': {'content': self._c}, 'finish_reason': self._f}]}

class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []
    def post(self, url, json=None, timeout=None):
        self.payloads.append(json)
        return self.responses.pop(0)

df = pd.DataFrame({'english': ['Hello', 'World [mc_name]', 'BWAHAHAHA'], 'thai': ['', '', '']})
rows = [(0, 'Hello'), (1, 'World [mc_name]'), (2, 'BWAHAHAHA')]
# attempt 1: line 2 valid, others bad; attempt 2: line 1 fixed, line 3 still bad; attempt 3: line 3 still bad
sess = FakeSession([
    FakeResp('1. \n2. โลก ⟦1⟧\n3. '),          # only line 2 valid
    FakeResp('1. สวัสดี\n2. '),                            # line 1 (Hello) fixed, line 3 empty
    FakeResp('1. '),                                       # line 3 never succeeds
])
logs = []
out = _translate_batch(sess, 'http://x', 'm', '', df, rows,
                       CharacterMemory(), TerminologyDB(''),
                       1500, 0.2, 0.9, 0.05, 0, threading.Event(),
                       logs.append, 0)
check('integration: valid line kept', out[1] == 'โลก [mc_name]')
check('integration: retried line fixed', out[0] == 'สวัสดี')
check('integration: failed line -> English', out[2] == 'BWAHAHAHA')
check('integration: 3 requests made', len(sess.payloads) == 3)
# attempt 2 should only re-send the 2 unresolved lines
p2_user = sess.payloads[1]['messages'][-1]['content']
check('integration: retry sends only 2 lines', 'THESE 2 LINE(S)' in p2_user)
check('integration: temp nudged on retry', sess.payloads[1]['temperature'] > sess.payloads[0]['temperature'])

# 7. finish=length distrusts last line
sess2 = FakeSession([
    FakeResp('1. สวัสดี\n2. โลกที่ถูกตัดกลางประ', finish='length'),
    FakeResp('1. โลกทั้งใบ'),
    FakeResp('1. โลกทั้งใบ'),
])
df2 = pd.DataFrame({'english': ['Hello', 'World'], 'thai': ['', '']})
out2 = _translate_batch(sess2, 'http://x', 'm', '', df2, [(0, 'Hello'), (1, 'World')],
                        CharacterMemory(), TerminologyDB(''),
                        1500, 0.2, 0.9, 0.05, 0, threading.Event(),
                        logs.append, 1)
check('length: first line accepted', out2[0] == 'สวัสดี')
check('length: truncated last line retried', out2[1] == 'โลกทั้งใบ')

print()
print('ALL PASS' if ok else 'SOME FAILED')
sys.exit(0 if ok else 1)
