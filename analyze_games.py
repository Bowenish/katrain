#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐手分析 SGF，用本地 KataGo(analysis 引擎)算出每一手的「目数损失(points lost)」，
按黑/白两方统计：多少手偏离最优、平均/累计损失多少目、有几手大失误，并列出最亏的若干手。

方法（和 KaTrain 的 points-lost 一致）：
  以「黑方领先目数(scoreLead, 视角=BLACK)」为准。对第 k 手（由某方走）：
    走之前局面的黑领先 = sl[k]，走之后的黑领先 = sl[k+1]
    黑走：loss = max(0, sl[k]   - sl[k+1])   # 黑走完黑领先掉了多少 = 黑亏
    白走：loss = max(0, sl[k+1] - sl[k])     # 白走完黑领先反升多少 = 白亏
  sl[k] 来自 KataGo 对「走了前 k 手的局面」在充分搜索后的评估（≈最优续着下的领先），
  所以它天然是「最优基准」，实际走的手偏离它多少，就是这手亏的目数。

重要说明（务必读）：
  * 「分析耗时」与「是否放水」无关，别用耗时推断。真正的指标就是这里算的每手目数损失。
  * 这两局都是【让 2 子】：白方(KataGo)先天让 2 子处于劣势，让子棋里白方经常主动
    「损目求乱」制造复杂局面，这类非最优手是让子棋策略，不能简单等同于「放水/被操纵」。
  * 实战网棋 bot 的搜索强度(visits)未知，可能远低于满血；复盘用的 visits 也不同，
    这些都会影响「最优手」的判定。数据看趋势即可，别当成「作弊铁证」。

用法：
  # 先确保用的是和实战一样的模型（b40 zhizi），二选一：
  set KATAGO_MODEL=C:\\Users\\duanb\\Downloads\\Go\\katrain\\katrain\\models\\kata1-zhizi-b40c768nbt-s11272M-d5935M.bin.gz
  # 然后：
  python analyze_games.py                      # 分析 test 目录下所有 sgf，默认 400 visits/手
  python analyze_games.py --visits 200         # 快一点（精度略低）
  python analyze_games.py --dry-run            # 只解析棋谱、打印手数，不启动引擎（验证解析）
  python analyze_games.py --max-moves 60       # 只分析前 60 手（快速验证脚本能跑通）
"""
import os, sys, json, glob, argparse, subprocess, threading, time, re

# Windows 控制台默认 cp1252，打印中文会报 UnicodeEncodeError → 强制 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))                 # ...\katrain
GO_ROOT = os.path.dirname(BASE)                                   # ...\Go
GTP_COLS = "ABCDEFGHJKLMNOPQRST"                                  # 跳过 I

# ---------------- 引擎/模型定位 ----------------
def find_engine():
    exe = os.path.join(GO_ROOT, "weiqi-web", "engine", "katago.exe")
    cfg = os.path.join(GO_ROOT, "weiqi-web", "engine", "analysis_config.cfg")
    return exe, cfg

def find_model(cli_model):
    if cli_model:
        return cli_model
    env = os.environ.get("KATAGO_MODEL")
    if env and os.path.exists(env):
        return env
    # 优先用实战同款 b40 zhizi
    for pat in ("*zhizi*b40*.bin.gz", "*b40*.bin.gz", "*.bin.gz"):
        cands = sorted(glob.glob(os.path.join(GO_ROOT, "katrain", "katrain", "models", pat)))
        if cands:
            return cands[0]
    cands = sorted(glob.glob(os.path.join(GO_ROOT, "weiqi-web", "engine", "*.bin.gz")))
    return cands[0] if cands else None

# ---------------- SGF 主线解析 ----------------
def sgf_to_gtp(coord, size=19):
    if not coord or len(coord) < 2:
        return "pass"
    c = ord(coord[0]) - ord('a')
    r = ord(coord[1]) - ord('a')
    if c < 0 or r < 0 or c >= size or r >= size:
        return "pass"                       # 越界(如 19 路的 tt) = 停一手
    return f"{GTP_COLS[c]}{size - r}"

def parse_sgf_mainline(text):
    """只走主线(每个分叉取第一支=实战)。返回 (ab, aw, moves)；moves=[('B','pd'),...]。"""
    ab, aw, moves = [], [], []
    n = len(text)

    def skip_ws(i):
        while i < n and text[i] in " \t\r\n":
            i += 1
        return i

    def read_props(i):
        # i 指向 ';' 之后，读该节点的所有 KEY[val]... 直到遇到 ; ( )
        while True:
            i = skip_ws(i)
            if i >= n or text[i] in ";()":
                return i
            key = ""
            while i < n and text[i].isalpha():
                key += text[i]; i += 1
            vals = []
            i = skip_ws(i)
            while i < n and text[i] == '[':
                j = i + 1; val = ""
                while j < n and (text[j] != ']' or text[j-1] == '\\'):
                    val += text[j]; j += 1
                vals.append(val)
                i = j + 1
                i = skip_ws(i)
            if key == "AB": ab.extend(vals)
            elif key == "AW": aw.extend(vals)
            elif key == "B": moves.append(('B', vals[0] if vals else ''))
            elif key == "W": moves.append(('W', vals[0] if vals else ''))

    def skip_tree(i):
        # i 指向 '('，跳过整棵子树(含内部注释里的括号)
        depth = 0
        while i < n:
            ch = text[i]
            if ch == '[':
                i += 1
                while i < n and (text[i] != ']' or text[i-1] == '\\'):
                    i += 1
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return i

    def parse_seq(i):
        # i 指向 '('
        i += 1
        while i < n:
            i = skip_ws(i)
            if i >= n:
                break
            ch = text[i]
            if ch == ';':
                i = read_props(i + 1)
            elif ch == '(':
                i = parse_seq(i)                 # 第一支 = 主线
                while True:                       # 跳过其余兄弟变化
                    i = skip_ws(i)
                    if i < n and text[i] == '(':
                        i = skip_tree(i)
                    else:
                        break
                return i
            elif ch == ')':
                return i + 1
            else:
                i += 1
        return i

    start = text.index('(')
    parse_seq(start)
    return ab, aw, moves

def parse_header(text):
    def tag(t):
        m = re.search(r'\b' + t + r'\[([^\]]*)\]', text)
        return m.group(1) if m else ""
    return {"PB": tag("PB"), "PW": tag("PW"), "RE": tag("RE"),
            "HA": tag("HA"), "KM": tag("KM"), "GN": tag("GN"), "DT": tag("DT")}

# ---------------- KataGo analysis 交互 ----------------
class Katago:
    def __init__(self, exe, cfg, model):
        self.exe, self.cfg, self.model = exe, cfg, model
        self.proc = None

    def start(self):
        cwd = os.path.dirname(self.exe)   # DLL 都在 exe 目录
        cmd = [self.exe, "analysis", "-model", self.model, "-config", self.cfg]
        print(f"[katago] 启动引擎（预热可能要十几秒）\n         模型 {os.path.basename(self.model)}")
        self.proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1)

    def analyze(self, query, need_turns, timeout=600):
        """发一个 query，收齐 need_turns 个 turn 的结果。返回 {turnNumber: response}。"""
        self.proc.stdin.write(json.dumps(query) + "\n")
        self.proc.stdin.flush()
        got = {}
        deadline = time.time() + timeout
        last = time.time()
        while len(got) < need_turns:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("引擎意外退出（stdout 关闭）。请检查显卡驱动/模型文件。")
            line = line.strip()
            if not line:
                if time.time() - last > timeout:
                    raise RuntimeError("引擎长时间无响应，超时。")
                continue
            try:
                resp = json.loads(line)
            except Exception:
                continue
            if "error" in resp:
                raise RuntimeError("KataGo 报错：" + str(resp))
            if resp.get("id") != query["id"]:
                continue
            tn = resp.get("turnNumber")
            if tn is not None:
                got[tn] = resp
                last = time.time()
                sys.stdout.write(f"\r         已分析 {len(got)}/{need_turns} 个局面…")
                sys.stdout.flush()
            if time.time() > deadline:
                raise RuntimeError("整局分析超时。可降低 --visits 或分批。")
        sys.stdout.write("\n")
        return got

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
        except Exception:
            pass

# ---------------- 单局分析 ----------------
def top_move(resp):
    mis = resp.get("moveInfos") or []
    if not mis:
        return None
    best = min(mis, key=lambda m: m.get("order", 999))
    return best.get("move")

def scorelead(resp):
    ri = resp.get("rootInfo") or {}
    return ri.get("scoreLead")

def json_path_for(sgf_path):
    return os.path.splitext(sgf_path)[0] + ".analysis.json"

def compute_records(kata, path, visits, komi, rules, max_moves, model):
    """跑 KataGo，返回 (meta, recs)。recs 每手 = {no,color,played,best,rank,loss,sl_before,sl_after}。"""
    text = open(path, "r", encoding="utf-8", errors="replace").read()
    hd = parse_header(text)
    ab, aw, moves = parse_sgf_mainline(text)
    if max_moves and len(moves) > max_moves:
        moves = moves[:max_moves]
    size = 19
    init = [["B", sgf_to_gtp(p, size)] for p in ab] + [["W", sgf_to_gtp(p, size)] for p in aw]
    gmoves = [[c, sgf_to_gtp(p, size)] for (c, p) in moves]
    M = len(gmoves)
    ai_side = "W" if ("KataGo" in hd["PW"] or "kata" in hd["PW"].lower()) else "B"
    print(f"\n[{os.path.basename(path)}] 跑引擎分析（{M} 手，visits={visits}）…")
    query = {
        "id": "g_" + os.path.basename(path),
        "initialStones": init, "moves": gmoves,
        "rules": rules, "komi": komi,
        "boardXSize": size, "boardYSize": size,
        "analyzeTurns": list(range(M + 1)), "maxVisits": visits,
    }
    resp = kata.analyze(query, need_turns=M + 1)
    sl = {t: scorelead(resp[t]) for t in range(M + 1) if t in resp and scorelead(resp[t]) is not None}
    recs = []
    for k in range(M):
        if k not in sl or (k + 1) not in sl:
            continue
        col = gmoves[k][0]
        before, after = sl[k], sl[k + 1]
        loss = max(0.0, (before - after) if col == "B" else (after - before))
        rank = None
        for m in (resp[k].get("moveInfos") or []):
            if m.get("move") == gmoves[k][1]:
                rank = m.get("order"); break
        recs.append({"no": k + 1, "color": col, "played": gmoves[k][1],
                     "best": top_move(resp[k]), "rank": rank,
                     "loss": round(loss, 3), "sl_before": round(before, 2), "sl_after": round(after, 2)})
    meta = {"sgf": os.path.basename(path), "GN": hd["GN"], "PB": hd["PB"], "PW": hd["PW"],
            "RE": hd["RE"], "HA": hd["HA"], "ai_side": ai_side, "moves_total": M,
            "visits": visits, "komi": komi, "rules": rules, "model": os.path.basename(model or "")}
    return meta, recs

def save_analysis(sgf_path, meta, recs):
    jp = json_path_for(sgf_path)
    with open(jp, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "moves": recs}, f, ensure_ascii=False, indent=1)
    print(f"  ✔ 已保存 → {os.path.basename(jp)}（以后直接读它，不用再跑引擎）")

def load_analysis(sgf_path):
    with open(json_path_for(sgf_path), "r", encoding="utf-8") as f:
        d = json.load(f)
    return d["meta"], d["moves"]

def _stats(rs):
    n = len(rs)
    if not n:
        return {"n": 0, "first_rate": 0, "nonbest": 0, "nonbest_rate": 0, "mean_loss": 0, "sum_loss": 0, "nb_loss": 0}
    first = sum(1 for r in rs if r["rank"] == 0)
    nb = [r for r in rs if r["rank"] != 0]
    return {"n": n, "first_rate": 100*first/n, "nonbest": len(nb), "nonbest_rate": 100*len(nb)/n,
            "mean_loss": sum(r["loss"] for r in rs)/n, "sum_loss": sum(r["loss"] for r in rs),
            "nb_loss": (sum(r["loss"] for r in nb)/len(nb)) if nb else 0.0}

def report(meta, recs, segment):
    ai_side = meta["ai_side"]; human_side = "B" if ai_side == "W" else "W"
    print("\n" + "=" * 72)
    print(f"棋谱：{meta['sgf']}")
    print(f"对局：{meta.get('GN','')}")
    print(f"黑 PB={meta['PB']}  白 PW={meta['PW']}  让子 HA={meta['HA']}  结果 RE={meta['RE']}")
    print(f"AI = {'白' if ai_side=='W' else '黑'}方；visits={meta['visits']}  模型={meta['model']}")
    print("=" * 72)

    def rank_dist(rs, label):
        n = len(rs) or 1
        cnt = lambda pred: sum(1 for r in rs if pred(r))
        b = [cnt(lambda r, k=k: r["rank"] == k) for k in range(4)]
        b59 = cnt(lambda r: r["rank"] is not None and 4 <= r["rank"] <= 8)
        b10 = cnt(lambda r: r["rank"] is not None and r["rank"] >= 9)
        bN = cnt(lambda r: r["rank"] is None)
        st = _stats(rs)
        print(f"\n{label}（共 {len(rs)} 手）")
        print(f"  ── 推荐排名分布 ──")
        for k in range(4):
            print(f"    第{k+1}推荐{'(最优)' if k==0 else '    '}：{b[k]:>3} 手（{100*b[k]/n:>4.0f}%）")
        print(f"    第5–9推荐    ：{b59:>3} 手（{100*b59/n:>4.0f}%）")
        print(f"    第10名开外   ：{b10:>3} 手     未进候选：{bN} 手")
        print(f"    → 非第1推荐 {st['nonbest']} 手（{st['nonbest_rate']:.0f}%），平均只损 {st['nb_loss']:.2f} 目")
        print(f"  ── 目数损失 ──  平均每手 {st['mean_loss']:.2f} 目 · 累计 {st['sum_loss']:.1f} 目 · "
              f"大失误(>3目) {sum(1 for r in rs if r['loss']>3)} 手")

    ai = [r for r in recs if r["color"] == ai_side]
    hu = [r for r in recs if r["color"] == human_side]
    rank_dist(ai, f"【AI（{'白' if ai_side=='W' else '黑'}方）】")
    rank_dist(hu, f"【人类 {meta['PB'] if human_side=='B' else meta['PW']}（{'黑' if human_side=='B' else '白'}方）】")

    # —— 按走棋顺序分段：看 AI 是否开局阶段就集中偏离最优 ——
    maxno = max((r["no"] for r in recs), default=0)
    print(f"\n【按走棋顺序分段（每 {segment} 手）· 开局在最上面几行】")
    print(f"     手数段   |  AI第1率  AI均损  AI累损  |  人类第1率 人类均损")
    lo = 1
    while lo <= maxno:
        hi = lo + segment - 1
        a = [r for r in recs if lo <= r["no"] <= hi and r["color"] == ai_side]
        h = [r for r in recs if lo <= r["no"] <= hi and r["color"] == human_side]
        sa, sh = _stats(a), _stats(h)
        print(f"  {lo:>3}-{hi:>3}手  |  {sa['first_rate']:>5.0f}%  {sa['mean_loss']:>6.2f}  {sa['sum_loss']:>6.1f}  |"
              f"  {sh['first_rate']:>5.0f}%  {sh['mean_loss']:>6.2f}")
        lo = hi + 1

    worst = sorted(ai, key=lambda r: -r["loss"])[:10]
    print(f"\n  AI 最亏的 10 手（手序·实走→推荐·排名·损目）：")
    for r in worst:
        rk = "未进候选" if r["rank"] is None else f"第{r['rank']+1}推荐"
        print(f"    第{r['no']:>3}手 {r['color']} {r['played']:>4} → 推荐 {str(r['best']):>4}（{rk}）  -{r['loss']:.1f}目")

# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(BASE, "test"), help="SGF 目录")
    ap.add_argument("--visits", type=int, default=400, help="每手搜索次数（越大越准越慢）")
    ap.add_argument("--komi", type=float, default=7.5)
    ap.add_argument("--rules", default="chinese")
    ap.add_argument("--model", default=None, help="模型路径（默认取 KATAGO_MODEL 或 b40 zhizi）")
    ap.add_argument("--max-moves", type=int, default=0, help="只分析前 N 手（0=全部）")
    ap.add_argument("--segment", type=int, default=40, help="按走棋顺序分段的每段手数")
    ap.add_argument("--force", action="store_true", help="忽略已存 JSON，强制重跑引擎")
    ap.add_argument("--report-only", action="store_true", help="只读已存 JSON 出报告，绝不跑引擎")
    ap.add_argument("--dry-run", action="store_true", help="只解析棋谱打印手数，不启动引擎")
    args = ap.parse_args()

    sgfs = sorted(glob.glob(os.path.join(args.dir, "*.sgf")))
    if not sgfs:
        print("没找到 SGF：" + args.dir); return

    if args.dry_run:
        for p in sgfs:
            text = open(p, "r", encoding="utf-8", errors="replace").read()
            hd = parse_header(text); ab, aw, moves = parse_sgf_mainline(text)
            print(f"{os.path.basename(p)}: PB={hd['PB']} PW={hd['PW']} HA={hd['HA']} RE={hd['RE']} 主线手数={len(moves)}")
        return

    # 需要跑引擎的：没有 .analysis.json 或指定了 --force；--report-only 一律不跑
    todo = [] if args.report_only else [p for p in sgfs if args.force or not os.path.exists(json_path_for(p))]
    if todo:
        exe, cfg = find_engine(); model = find_model(args.model)
        for pth, what in ((exe, "katago.exe"), (cfg, "config"), (model, "模型")):
            if not pth or not os.path.exists(pth):
                print(f"❌ 找不到 {what}: {pth}"); return
        kata = Katago(exe, cfg, model); kata.start()
        try:
            for p in todo:
                meta, recs = compute_records(kata, p, args.visits, args.komi, args.rules, args.max_moves, model)
                save_analysis(p, meta, recs)
        finally:
            kata.close()
    else:
        print("（都已有分析数据，直接读 JSON 出报告，不跑引擎）")

    for p in sgfs:
        if not os.path.exists(json_path_for(p)):
            print(f"\n（跳过 {os.path.basename(p)}：还没有分析数据，去掉 --report-only 跑一次即可生成）"); continue
        meta, recs = load_analysis(p)
        report(meta, recs, args.segment)

    print("\n" + "-" * 72)
    print("解读提醒：")
    print(" • 两局都让 2 子；让子对人的 bot 常不追求每手目数最优，非第1推荐≠放水送目。")
    print(" • 排名(第几推荐)比目数损失更依赖 visits；实战 bot 搜索强度未知，看趋势。")
    print(" • 数据已存 <棋谱名>.analysis.json，下次 `--report-only` 或换 `--segment N` 秒出报告，不用再跑引擎。")

if __name__ == "__main__":
    main()
