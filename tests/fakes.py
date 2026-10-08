"""A fake OpenAI-compatible client for judgment tests. No network."""
import json
import threading
from types import SimpleNamespace as NS

from app.models import CATEGORIES

NVIDIA_LIKE = dict(zip(CATEGORIES, [9.7, 4.2, 7.1, 7.8, 9.0, 8.3]))


def payload(scores=None, **overrides):
    scores = scores or NVIDIA_LIKE
    items = [{"category": c, "score": s, "justification": f"why {c}", "evidence_links": ["https://example.com/" + c]}
             for c, s in scores.items()]
    data = {"scores": items}
    data.update(overrides)
    return json.dumps(data)


class FakeClient:
    """Each entry in `responses` is a str (message content) or an Exception to raise."""

    def __init__(self, *responses, model="grok-4.3-fake"):
        self.responses = list(responses)
        self.model = model
        self.calls = []
        self.threads = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        self.threads.append(threading.current_thread().name)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        usage = NS(prompt_tokens=250, completion_tokens=900, total_tokens=1150,
                   completion_tokens_details=NS(reasoning_tokens=400), cost_in_usd_ticks=37_500_000)
        return NS(model=self.model, usage=usage,
                  choices=[NS(message=NS(content=item), finish_reason="stop")])
