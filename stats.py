import math
from html import escape

Z = 1.96
ALPHA = 0.05


def gamma_q(a, x):
    if x <= 0:
        return 1.0
    log_pref = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1:
        term = total = 1.0 / a
        ap = a
        for _ in range(1000):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(log_pref))
    tiny = 1e-300
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return min(1.0, math.exp(log_pref) * h)


def chi2_sf(x, df):
    return gamma_q(df / 2, x / 2)


def wilson(c, n):
    if n == 0:
        return 0.0, 0.0
    p = c / n
    z2 = Z * Z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = Z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def uniformity(counts):
    n = sum(counts)
    k = len(counts)
    expected = n / k
    chi = sum((c - expected) ** 2 / expected for c in counts)
    return chi, chi2_sf(chi, k - 1), expected


def entropy_ratio(counts):
    n = sum(counts)
    h = -sum(c / n * math.log2(c / n) for c in counts if c)
    return h / math.log2(len(counts))


def longest_run(values):
    best_len, best_val = 0, None
    cur_len, prev = 0, None
    for v in values:
        if v == prev:
            cur_len += 1
        else:
            cur_len, prev = 1, v
        if cur_len > best_len:
            best_len, best_val = cur_len, v
    return best_len, best_val


def serial_independence(values, k):
    pairs = len(values) - 1
    if pairs < 5 * k * k:
        return None
    table = [[0] * k for _ in range(k)]
    for a, b in zip(values, values[1:]):
        table[a - 1][b - 1] += 1
    rows = [sum(r) for r in table]
    cols = [sum(table[i][j] for i in range(k)) for j in range(k)]
    chi = 0.0
    for i in range(k):
        for j in range(k):
            e = rows[i] * cols[j] / pairs
            if e > 0:
                chi += (table[i][j] - e) ** 2 / e
    df = (sum(1 for r in rows if r) - 1) * (sum(1 for c in cols if c) - 1)
    if df <= 0:
        return None
    return chi2_sf(chi, df)


def fmt_p(p):
    return "<0.001" if p < 0.001 else f"{p:.3f}"


def build_report(title, scope, values, k, names):
    values = [v for v in values if 1 <= v <= k]
    n = len(values)
    head = f"📊 <b>{escape(title)}</b>\n{escape(scope)}: <b>{n}</b>\n\n"
    if n == 0:
        return head + "Данных пока нет."

    counts = [0] * k
    for v in values:
        counts[v - 1] += 1

    lines = [f"{'Значение':<16}{'N':>5}{'%':>7}  {'95% ДИ':<9}", "-" * 38]
    for v in range(1, k + 1):
        c = counts[v - 1]
        lo, hi = wilson(c, n)
        label = f"{v} {names.get(str(v), '?')}"[:15]
        ci = f"{lo * 100:.0f}-{hi * 100:.0f}"
        lines.append(f"{label:<16}{c:>5}{c / n * 100:>7.1f}  {ci:<9}")

    chi, p, expected = uniformity(counts)
    lines.append("")
    lines.append(f"Ожидается: {100 / k:.1f}% на значение")
    lines.append(f"chi2={chi:.2f}, p={fmt_p(p)}")
    if expected < 5:
        lines.append("Мало данных: тест ненадёжен")
    elif p < ALPHA:
        lines.append("Есть отклонение от равномерного")
    else:
        lines.append("Отклонений от равномерного нет")
    lines.append(f"Энтропия: {entropy_ratio(counts) * 100:.1f}% от макс.")
    run_len, run_val = longest_run(values)
    lines.append(f"Макс. серия: {run_len} x значение {run_val}")
    sp = serial_independence(values, k)
    if sp is None:
        lines.append("Связь соседних бросков: мало данных")
    else:
        verdict = "есть связь" if sp < ALPHA else "связи нет"
        lines.append(f"Связь соседних бросков: p={fmt_p(sp)}, {verdict}")

    return head + "<pre>" + escape("\n".join(lines)) + "</pre>"
