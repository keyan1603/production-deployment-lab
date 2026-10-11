"""Write the release manifests the studies use (releases/*.json).

r1-baseline          exactly the prompts and model pins the system ships with
r2-shorter-reply     a benign change: the customer reply goes from 3 sentences to 2
r3-helpful-answerer  a well-meant change that should be refused: the knowledge assistant may "use general
                     knowledge" when the sources fall short, instead of saying it cannot find the answer
r4-renamed-categories  a format change that breaks a contract: triage categories renamed in the prompt, while
                     the code still validates against the old names
r5-model-typo        the baseline with a mistyped light-model pin
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from deploylab.releases import Registry, Release, current_prompts, lint  # noqa: E402

MODELS = {"light": "gemini-3.5-flash-lite", "heavy": "gemini-3.5-flash"}
BASE = current_prompts()


def derive(release_id, notes, prompts=None, models=None):
    return Release(id=release_id, parent="r1-baseline", notes=notes, prompts={**BASE, **(prompts or {})}, models=models or MODELS)


releases = [
    Release(id="r1-baseline", parent=None, notes="The prompts and model pins the system ships with.", prompts=BASE, models=MODELS),
    derive("r2-shorter-reply", "Benign: customer reply shortened from 3 sentences to 2.",
           {"responder": BASE["responder"].replace("(3 sentences)", "(2 sentences)")}),
    derive("r3-helpful-answerer", "Well meant, should be refused: lets the knowledge assistant fall back on general knowledge.",
           {"answerer": ("You answer questions using the numbered sources provided in the user message when they help. "
                         "Cite sources in square brackets, for example [HR-002]. Quote exact numbers and names from the sources. "
                         "If the sources do not fully answer the question, use your general knowledge to give the most helpful answer you can.")}),
    derive("r4-renamed-categories", "Breaks a contract: triage categories renamed in the prompt, code still expects the old ones.",
           {"triage": ('You triage customer support tickets. Reply with ONLY a JSON object, no prose, with keys: '
                       '"category" (payments, tech or access), "severity" (low, medium or high), '
                       '"customer_id" (like C-1001, or null), "order_id" (like O-5001, or null), '
                       'and "summary" (one sentence that restates the problem and keeps any customer or order ids).')}),
    derive("r5-model-typo", "Baseline with a mistyped light-model pin.", models={**MODELS, "light": "gemini-3.5-flash-lit"}),
]

registry = Registry(pathlib.Path(__file__).resolve().parent.parent / "releases")
for rel in releases:
    problems = lint(rel)
    assert not problems, (rel.id, problems)
    registry.save(rel)
    print(f"{rel.id:24s} digest {rel.digest}  parent {rel.parent}")
