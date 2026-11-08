import asyncio


class Pacer:
    def __init__(self, start=0.3, floor=0.15, ceil=1.5):
        self.delay = start
        self.floor = floor
        self.ceil = ceil
        self.streak = 0

    def success(self):
        self.streak += 1
        if self.streak >= 15:
            self.delay = max(self.floor, self.delay * 0.85)
            self.streak = 0

    def flood(self):
        self.delay = min(self.ceil, self.delay * 1.6)
        self.streak = 0


async def nap(seconds, stop):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def collect(send, total, pacer, stop, on_progress=None, max_errors=5):
    out = []
    errors = 0
    while len(out) < total and not stop.is_set() and errors < max_errors:
        try:
            item = await send()
        except Exception as e:
            wait = getattr(e, "retry_after", None)
            if wait is not None:
                pacer.flood()
                await nap(wait + 1, stop)
            else:
                errors += 1
                await nap(1, stop)
            continue
        out.append(item)
        pacer.success()
        if on_progress:
            await on_progress(len(out))
        await nap(pacer.delay, stop)
    return out, errors


async def collect_missing(send, wanted, pacer, stop, cap=80):
    found = {}
    left = set(wanted)
    errors = 0
    tries = 0
    while left and not stop.is_set() and tries < cap and errors < 5:
        try:
            value, message_id = await send()
        except Exception as e:
            wait = getattr(e, "retry_after", None)
            if wait is not None:
                pacer.flood()
                await nap(wait + 1, stop)
            else:
                errors += 1
                await nap(1, stop)
            continue
        tries += 1
        pacer.success()
        if value in left:
            left.discard(value)
            found[value] = message_id
        await nap(max(pacer.delay, 0.6), stop)
    return found
