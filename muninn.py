# modules/muninn/muninn.py
"""
Muninn - the UI over the Muninn memory tool (tools/muninn): capture anything, see it by thread, pick a thread back up.
Views: Today (@commands waiting for one tap, pinned and dated next steps, slipped ones shown once, parked ones out of view, what the assistant is waiting on you for, what arrived while you were away), Recent (newest captures),
Pads (free scratch text in tabs - the portal's TabManager - saved as you type, kept until its content is dealt with), Pile (closed pads),
Thread (card, the assistant's proposed card update, next steps, tried, questions, timeline), Search, Import (a Markdown document split at its ## headings into thread pages), Huginn (the assistant: settings, charter, log).
Everything is stored by the tool, per login; this module holds only which view each person had open.
"""
import json, secrets, difflib
from datetime import datetime, timedelta
from fastapi import APIRouter, Request

MODULE_META = {"label": "Muninn", "icon": "&#x1F426;", "description": "Capture anything, find it again, pick a thread back up"}
router = APIRouter()
ENV = {}
UI = BI = IM = MN = HG = TM = None

def init_module(env: dict):
    global UI, BI, IM, MN, HG, TM
    ENV.update(env)
    UI, BI, MN = env["templates"].env.globals.get("UI"), env["tools"]["built_ins"], env["tools"]["muninn"]
    HG = MN.huginn
    IM = env["InterfaceManager"](nesting_level=1, db_path="muninn/im_registry.db")
    TM = BI.TabManager(namespace="muninn_pads", tab_bar_id="mn-pad-tabs", content_id="mn-pad-body", render_content_fn=_pad_render, intent_prefix="muninn_pads", IM=IM, nesting_level=1,
                       empty={"tabs": {"new": {"id": "new", "label": "New pad", "order": 0}}, "active": "new"})   # "new" is a placeholder the render replaces with a real pad, so the tab list is never empty
    IM.scripts.update({f"muninn_{k}": [fn] for k, fn in {"view": _h_view, "capture": _h_capture, "link": _h_link, "next": _h_next, "pin": _h_pin, "next_set": _h_next_set, "card_edit": _h_card_edit, "card_save": _h_card_save, "card_answer": _h_card_answer, "import": _h_import,
                       "pad_save": _h_pad_save, "pad_set": _h_pad_set, "pad_delete": _h_pad_delete, "pad_note": _h_pad_note, "pad_answer": _h_pad_answer, "suggest": _h_suggest, "cmd": _h_cmd,
                       "huginn_save": _h_huginn_save, "huginn_refresh": _h_huginn_refresh, "huginn_me": _h_huginn_me, "charter_save": _h_charter_save, "huginn_run": _h_huginn_run, "survey_me": _h_survey_me, "survey_settings": _h_survey_settings, "absorb": _h_absorb, "moot_keep": _h_moot_keep, "reduce_set": _h_reduce_set, "reduce_destroy": _h_reduce_destroy, "reduce_seal": _h_reduce_seal, "archive": _h_archive, "pads_open_tab": _h_pad_tab_new, "pads_close_tab": _h_pad_tab_close}.items()})

# --- HTML helpers ---

def _post(action: str, **extra) -> str: return f"""hx-post="/im/in" hx-target="body" hx-swap="none" hx-vals='{json.dumps({"type": f"muninn_{action}", "lvl": 1, **extra})}'"""
def _user(request) -> str: return request.state.user.username
def _when(t: str) -> str: return t[5:16].replace("T", " ")
def _chip(s: str) -> str: return f"""<button class="chip" {_post("view", view="thread", slug=s)}>#{UI.escape(s)}</button>"""
def _empty(text: str) -> str: return f"""<div class="list-empty">{text}</div>"""

def _text_html(text: str, plain: bool = False) -> str: return "".join(f"""<div class="mn-l-{"text" if plain else MN.line_kind(l)}">{UI.escape(l) or "&nbsp;"}</div>""" for l in text.splitlines())

def _capture_form(thread: str = "") -> str:
    specs = MN.command_specs()
    cmds = " ".join(sorted({"@" + s["service"] for s in specs.values()}))
    hint = f"Add to #{thread} - other #threads, > next, ? question, tried: ..." if thread else f"""Anything - #thread, > next step, ? question, tried: ...{", " + cmds + " ..." if cmds else ""} (nothing is required)"""
    helps = "\n".join(sorted({s["help"] for s in specs.values()}))
    return f"""<form class="node-form" {_post("capture", thread=thread)}><textarea name="text" class="cm-input" rows="3" placeholder="{UI.escape(hint)}" title="{UI.escape(helps)}" required></textarea><div class="row"><span class="grow"></span><button type="submit" class="ui-btn">Save</button></div></form>"""

def _entry_html(e: dict) -> str:
    ctx = f"""<span class="status-label" title="{UI.escape(e["ctx"].get("path", ""))}">{UI.escape(e["ctx"]["tab"])}</span>""" if (e.get("ctx") or {}).get("tab") else ""
    link = f"""<details class="mn-link"><summary title="Add to a thread">+#</summary><form {_post("link", entry=e["id"])}><input name="thread" list="mn-thread-list" class="module-select" placeholder="thread name" required></form></details>"""
    absorb = f"""<button class="chip" title="{"In a card since " + _when(e["absorbed"]) + " - press to keep it in the live notes" if e["absorbed"] else "Its content is in a thread card now (it can leave the live notes after a grace period - Reduce)"}" {_post("absorb", entry=e["id"], on=0 if e["absorbed"] else 1)}>{"&#x2714; in card" if e["absorbed"] else "in card?"}</button>"""
    ran = "".join(f"""<div class="status-label">{"&#x2713; ran" if r["op"] == "cmd_run" else "&#x2715; dismissed"}: {UI.escape(r.get("text") or e["commands"][i])}{" &#x2192; " + UI.escape(r["result"]) if r.get("result") else ""} ({_when(r["t"])})</div>""" for i, r in sorted(e["cmd"].items()))
    return f"""<div class="list-row"{' style="opacity:.6"' if e["absorbed"] else ""}><div class="row status-label"><span>{_when(e["t"])}</span>{ctx}{"".join(_chip(s) for s in e["threads"])}{link}{absorb}</div><div class="pre-wrap">{_text_html(e["text"], e.get("plain", False))}</div>{ran}</div>"""

def _pin(e: dict, i: int) -> str: return f"""<button class="cm-qbtn" title="{"Unpin from Today" if i in e["pinned"] else "Pin to Today (focus, up to 3)"}" {_post("pin", entry=e["id"], index=i, pin=0 if i in e["pinned"] else 1)}>{"&#x2605;" if i in e["pinned"] else "&#x2606;"}</button>"""

def _ctl(e: dict, i: int) -> str:
    """Date, park, drop and how much it matters, for one next step, folded behind a calendar button."""
    unpark = f"""<button type="button" class="ui-btn" {_post("next_set", entry=e["id"], index=i, op="unpark")}>Unpark</button>""" if MN.is_parked(e, i) else ""
    levels = "".join(f"""<button type="submit" name="op" value="level_{lv}" class="ui-btn" title="Your rating - it wins over Huginn and teaches it">{lv}</button>""" for lv in MN.LEVELS)
    return f"""<details class="mn-link"><summary title="Due date, park until a date, or drop">&#x1F4C5;</summary><form class="row" {_post("next_set", entry=e["id"], index=i)}>
                   <input type="date" name="date" class="module-select"><button type="submit" name="op" value="due" class="ui-btn" title="Set or change its date (blank clears a date set here)">Due</button>
                   <button type="submit" name="op" value="park" class="ui-btn" title="Out of view until this date (blank = a week)">Park until</button>
                   <button type="submit" name="op" value="drop" class="ui-btn" title="Not needed after all - closes it">Drop</button>{unpark}
                   <span class="status-label">matters:</span>{levels}</form></details>"""

def _date_label(e: dict, i: int, n: str) -> str:
    d, d0 = MN.due(e, i, n), datetime.now().date()
    if MN.is_parked(e, i): return f"""<span class="status-label">parked until {e["parked"][i]}</span>"""
    return f"""<b>{d.strftime("%a %m-%d")}</b>{' <span class="status-label">(was due - still open)</span>' if d < d0 else ""}""" if d else ""

_SIG_COLOR = {"high": "#ff7a59", "some": "var(--accent)", "low": "var(--text_muted)"}

def _sig_html(sig: dict, e: dict, i: int) -> str:
    """How much it matters: a badge, and the reason in one line (the person's own rating is marked as such)."""
    r = sig.get(MN.skey(e, i))
    if not r: return ""
    who = "your rating" if r["author"] == "user" else "Huginn"
    wait = " - waits on something" if r.get("actionable") is False else ""
    why = f"""<span class="status-label">{UI.escape(r["why"])}{wait}</span>""" if r["why"] or wait else ""
    return f"""<span class="status-badge" style="color:{_SIG_COLOR[r["level"]]}" title="{who}{wait}">{r["level"]}</span>{why}"""

def _next_row(e: dict, i: int, n: str, chips: bool = True, sig: dict = None) -> str: return f"""<div class="row"><button class="cm-qbtn" title="Done" {_post("next", entry=e["id"], index=i, done=1)}>&#x2713;</button>{_pin(e, i)}{_date_label(e, i, n)}<span>{UI.escape(n)}</span>{_sig_html(sig or {}, e, i)}{"".join(_chip(s) for s in e["threads"]) if chips else ""}{_ctl(e, i)}</div>"""

def _next_html(items: list, slug: str = "", empty: str = "No open next steps.", sig: dict = None) -> str: return "".join(_next_row(e, i, n, not slug, sig) for e, i, n in items) or _empty(empty)
def _dated_html(items: list, empty: str, sig: dict = None) -> str: return "".join(_next_row(e, i, n, True, sig) for d, e, i, n in items) or _empty(empty)

def _section(title: str, body: str, open_: bool = True) -> str: return f"""<details class="sec"{" open" if open_ else ""}><summary>{title}</summary>{body}</details>"""

# --- Views ---

def _sugg_html(s: dict, es: dict) -> str:
    """One suggestion as a small form: the proposed thread and/or next step can be corrected before accepting (blank leaves that part out)."""
    e = es.get(s["entry"])
    thread = f"""<span class="status-label">#</span><input name="thread" class="module-select" list="mn-thread-list" value="{UI.escape(s["thread"])}" style="width:9rem">""" if s["thread"] else ""
    nxt = f"""<span class="status-label">&gt;</span><input name="next" class="module-select grow" value="{UI.escape(s["next"])}">""" if s["next"] else ""
    return f"""<form class="list-row" {_post("suggest", sid=s["id"])}><div class="row"><span class="grow">{UI.escape(e["text"][:160]) if e else "(capture no longer there)"}</span><span class="status-label">{UI.escape(s["reason"])}</span></div>
               <div class="row">{thread}{nxt}<button type="submit" name="accept" value="1" class="cm-qbtn" title="Accept (as edited)">&#x2713;</button><button type="submit" name="accept" value="0" class="cm-qbtn" title="No">&#x2715;</button></div></form>"""

def _cmd_row(e: dict, i: int, line: str, p: dict) -> str:
    """One command line waiting for a tap: the line (editable), what Run would do or why it cannot, then Run / check the edited line / dismiss."""
    what = f"""<span class="status-label" style="color:#ff5f5f">{UI.escape(p["error"])}</span>""" if p.get("error") else f"""<span class="status-label">{UI.escape(p["summary"])}</span>"""
    return f"""<form class="list-row" {_post("cmd", entry=e["id"], index=i)}><div class="row"><input name="line" class="module-select grow" value="{UI.escape(line)}"><button type="submit" name="op" value="run" class="ui-btn">Run</button><button type="submit" name="op" value="check" class="cm-qbtn" title="Show what it would do with the line as edited">?</button><button type="submit" name="op" value="dismiss" class="cm-qbtn" title="Not to be run - it stays in the capture as text">&#x2715;</button></div>
               <div class="row">{what}{"".join(_chip(s) for s in e["threads"])}<span class="status-label">{_when(e["t"])}</span></div></form>"""

async def _cmds_html(request, items: list) -> str: return "".join([f"""<div id="mn-cmd-{e["id"]}-{i}">{_cmd_row(e, i, c, await MN.command_preview(request, c, e))}</div>""" for e, i, c in items])

async def _today_html(request, note: str = "") -> str:
    user = _user(request)
    t, es = MN.today(user), {e["id"]: e for e in MN.entries(user)}
    cmds = await _cmds_html(request, t["commands"])
    since = f"""<div class="info-bar"><b>Since you were last here:</b> {len(t["since"])} new from the assistant, the agent or shared from another app (below in Recent).</div>""" if t["since"] else ""
    sugg = "".join(_sugg_html(s, es) for s in t["suggestions"][:15])
    moots = len(MN.moots(user))
    waiting = "".join(f"""<button class="chip" {_post("view", view="pad", slug=m["id"])}>pad: {UI.escape(m["title"])} ({c})</button>""" for m, c in t["proposals"]) + "".join(f"""<button class="chip" {_post("view", view="thread", slug=r["slug"])}>card: #{UI.escape(r["slug"])}</button>""" for r in t["cards"]) + (f"""<button class="chip" {_post("view", view="reduce")}>reduce: {moots} note(s) no longer needed?</button>""" if moots else "")
    surveys, money, d0 = _surveys_html(HG.surveys(user)), _due_elsewhere(user), datetime.now().date().isoformat()
    soon_day = (datetime.now().date() + timedelta(days=2)).isoformat()
    now_html = (_dated_html(t["now"], "", t["signals"]) if t["now"] else "") + _money_html([i for i in money if i["date"] <= soon_day], d0)
    soon_html = (_dated_html(t["soon"], "", t["signals"]) if t["soon"] else "") + _money_html([i for i in money if i["date"] > soon_day], d0)
    parked = "".join(f"""<div class="row"><span class="status-label">until {u}</span><span>{UI.escape(n)}</span>{"".join(_chip(s) for s in e["threads"])}<button class="cm-qbtn" title="Back now" {_post("next_set", entry=e["id"], index=i, op="unpark")}>&#x21A9;</button></div>""" for u, e, i, n in sorted(t["parked"], key=lambda x: x[0]))
    return f"""<div class="row"><span class="form-title">Today</span><span class="status-label">{datetime.now().strftime("%A %m-%d")}</span></div>{since}{note}{_capture_form()}
               {_section(f"Commands to run ({len(t['commands'])}) - one tap each; edit the line first if needed", cmds) if cmds else ""}
               {_section("Focus", _next_html(t["focus"], empty="Pin up to three next steps with &#x2606; to keep them here.", sig=t["signals"]))}
               {_section("Needs action now", now_html or _empty("Nothing dated within two days."))}
               {_section("Coming up (14 days)", soon_html or _empty("Nothing dated in the next two weeks."), bool(soon_html))}
               {_section(f"Slipped ({len(t['slipped'])}) - shown once on its day; done, a new date, park or drop when you can", _dated_html(t["slipped"], "Nothing slipped.", t["signals"]), False)}
               {_section(f"Suggestions from Huginn ({len(t['suggestions'])})", sugg or _empty("None waiting."), bool(sugg))}
               {_section("Waiting for your answer", f'<div class="row">{waiting}</div>' if waiting else _empty("Nothing waiting."), bool(waiting))}
               {_section("Agent surveys (3 days)", surveys, False) if surveys else ""}
               {_section(f"Parked ({len(t['parked'])})", parked or _empty("Nothing parked."), False)}"""

def _recent_html(user: str) -> str:
    return f"""<div class="row"><span class="form-title">Recent</span></div>{_capture_form()}
               {_section("Next steps", _next_html(MN.open_next(user)[:12], sig=MN.signals(user)))}
               {_section("Captures", "".join(_entry_html(e) for e in MN.entries(user, limit=60)) or _empty("Nothing captured yet."))}"""

def _card_prop_html(slug: str, cur: str, r: dict) -> str:
    """The assistant's proposed card: what changed (a line diff), the whole proposal, and accept / decline with a reply / edit first."""
    diff = "\n".join(list(difflib.unified_diff(cur.strip().splitlines(), r["body"].splitlines(), lineterm="", n=1))[2:])   # [2:] drops the ---/+++ file header lines
    body = BI.md_plus_transpiler(r["body"])
    return f"""<div class="info-bar" style="display:block"><div><b>Huginn proposes an updated card</b> <span class="status-label">{_when(r["t"])}{": " + UI.escape(r["reason"]) if r["reason"] else ""}</span></div>
               <details><summary class="status-label">What changes</summary><pre class="quote-block">{UI.escape(diff) or "(same text)"}</pre></details><details><summary class="status-label">Proposed card</summary><div class="glass" style="padding:.3rem .5rem">{body}</div></details>
               <form class="row" {_post("card_answer", slug=slug, prop=r["id"])}><input name="reply" class="module-select grow" placeholder="Reply (optional): what it missed, what to keep"><button type="submit" name="accept" value="1" class="ui-btn">Accept</button><button type="submit" name="accept" value="0" class="ui-btn">Decline</button><button type="button" class="ui-btn" {_post("card_edit", slug=slug, prop=r["id"])}>Edit first</button></form></div>"""

def _thread_html(user: str, slug: str, editing: bool = False, prop: str = "") -> str:
    p, es, props = MN.page(user, slug), MN.entries(user, slug), MN.card_props(user, slug)
    pr = next((r for r in props if r["id"] == prop), None)
    if editing:
        keep = f"""<input type="hidden" name="prop" value="{pr["id"]}"><span class="status-label">Editing Huginn's proposal - saving accepts it as you leave it.</span>""" if pr else ""
        card = f"""<form class="node-form" {_post("card_save", slug=slug)}>{keep}<input name="title" class="module-select" value="{UI.escape(p["title"])}"><textarea name="body" class="cm-input" rows="16">{UI.escape(pr["body"] if pr else p["body"])}</textarea><div class="form-actions"><button type="submit" class="ui-btn">Save</button><button type="button" class="ui-btn" {_post("view", view="thread", slug=slug)}>Cancel</button></div></form>"""
    else:
        body = BI.md_plus_transpiler(p["body"]) if p["body"].strip() else '<div class="list-empty">No card yet. Edit to write where this stands: state, decisions, the next small step.</div>'
        card = "".join(_card_prop_html(slug, p["body"], r) for r in props) + f"""<div class="glass" style="padding:.3rem .5rem">{body}</div>"""
    tried = "".join(f"""<div class="mn-l-tried">{UI.escape(t)} <span class="status-label">{_when(e["t"])}</span></div>""" for e in es for t in e["tried"])
    qs = "".join(f"""<div class="mn-l-question">{UI.escape(q)} <span class="status-label">{_when(e["t"])}</span></div>""" for e in es for q in e["questions"])
    edit_btn = "" if editing else f"""<button class="ui-btn" {_post("card_edit", slug=slug)}>Edit card</button>"""
    return f"""<div class="row"><span class="form-title">{UI.escape(p["title"])}</span><span class="status-label">#{UI.escape(slug)} - {len(es)} capture(s)</span>{edit_btn}</div>
               {card}
               {_section("Next steps", _next_html(MN.open_next(user, slug), slug, sig=MN.signals(user)))}
               {_section(f"Tried ({tried.count('mn-l-tried')})", tried or _empty("Nothing marked tried: yet."), bool(tried))}
               {_section(f"Questions ({qs.count('mn-l-question')})", qs or _empty("No open questions."), bool(qs))}
               {_capture_form(slug)}
               {_section("Timeline", "".join(_entry_html(e) for e in es) or _empty("No captures on this thread yet."))}"""

def _search_html(user: str, q: str) -> str:
    r = MN.search(user, q)
    pages = "".join(f"""<div class="row">{_chip(t["slug"])}<span>{UI.escape(t["title"])}</span></div>""" for t in r["pages"])
    return f"""<div class="row"><span class="form-title">Search: {UI.escape(q)}</span><span class="status-label">{len(r["pages"])} thread(s), {len(r["entries"])} capture(s)</span></div>
               {_section("Threads", pages or _empty("No thread matches."))}{_section("Captures", "".join(_entry_html(e) for e in r["entries"]) or _empty("No capture matches."))}"""

def _import_html(done: list = None) -> str:
    result = f"""<div class="info-bar">Imported into {len(done)} thread(s): {"".join(_chip(s) for s in done)}</div>""" if done is not None else ""
    return f"""<div class="row"><span class="form-title">Import</span></div>{result}
               <div class="list-empty">Paste a Markdown document. Each <b>## heading</b> becomes a thread (named by the heading) and its section that thread's card - added below the card if the thread already has one. Text before the first heading is saved as a capture.</div>
               <form class="node-form" {_post("import")}><textarea name="text" class="cm-input" rows="18" required></textarea><button type="submit" class="ui-btn">Import</button></form>"""

# --- Pads (tabs through the portal's TabManager: tab id = pad id; open pads are the tabs) ---

async def _pad_render(request, state: dict) -> tuple:
    """TabManager render hook: the tabs follow the open pads (a pad opened from the bridge or the pile appears; a closed one leaves), labels follow pad titles."""
    user = _user(request)
    opened = {m["id"]: m for m in MN.pads(user, "open")} or {(m := MN.pad_new(user))["id"]: m}
    tabs = {k: {**t, "label": opened[k]["title"]} for k, t in state["tabs"].items() if k in opened}
    for k, m in sorted(opened.items(), key=lambda kv: kv[1]["created"]): tabs.setdefault(k, {"id": k, "label": m["title"], "order": max([t.get("order", 0) for t in tabs.values()] + [-1]) + 1})
    state["tabs"] = tabs
    if state.get("active") not in tabs: state["active"] = sorted(tabs.values(), key=lambda t: t.get("order", 0))[-1]["id"]
    return state, _pad_body(user, state["active"])

async def _pads_html(request, focus: str = "") -> str:
    state = await TM._load(request)
    if focus: state["active"] = focus
    state, body = await _pad_render(request, TM.next_active(state))
    await TM._save(request, state)
    return f"""{await TM.tab_bar_fn(state, TM.tab_bar_id, TM.intent_prefix, 1, allow_new=True, closable=True)}<div id="mn-pad-body">{body}</div>"""

async def _h_pad_tab_new(request, payload, imr):
    """The tab bar's + (and the side bar's): a new pad, opened as the active tab. Rendered as the pads view so it works from any view."""
    return await _show(request, imr, {"view": "pad", "slug": MN.pad_new(_user(request))["id"]})

async def _h_pad_tab_close(request, payload, imr):
    """A tab's close: the pad goes to the pile (kept); the render drops its tab."""
    MN.pad_set(_user(request), payload["id"], status="closed")
    return await _show(request, imr, {"view": "pad"})

def _proposal_html(pid: str, r: dict) -> str:
    what = {"move": f"""move to #{UI.escape(r["target"])}""", "wiki": f"""add to the wiki page {UI.escape(r["target"])}""", "delete": "delete", "reduce": f"""reduce{" (original to #" + UI.escape(r["target"]) + ")" if r["target"] else ""}"""}[r["action"]]
    repl = f"""<div class="status-label">becomes:</div><pre class="quote-block">{UI.escape(r["replacement"])}</pre>""" if r["action"] == "reduce" else ""
    if r["status"] != "pending": return f"""<div class="list-row"><span class="status-label">{_when(r["t"])} {UI.escape(r["author"])}: {what} - {UI.escape(r["status"])}</span><pre class="quote-block">{UI.escape(r["block"][:300])}</pre>{f'<div class="status-label">your reply: {UI.escape(r["reply"])}</div>' if r.get("reply") else ""}</div>"""
    return f"""<div class="list-row"><div><b>{what}</b> <span class="status-label">{_when(r["t"])} {UI.escape(r["author"])}{": " + UI.escape(r["reason"]) if r["reason"] else ""}</span></div><pre class="quote-block">{UI.escape(r["block"])}</pre>{repl}
               <form class="row" {_post("pad_answer", slug=pid, prop=r["id"])}><input name="reply" class="module-select grow" placeholder="Reply (optional): why not, what it missed, what to keep"><button type="submit" name="accept" value="1" class="ui-btn">Accept</button><button type="submit" name="accept" value="0" class="ui-btn">Decline</button></form></div>"""

def _pad_body(user: str, pid: str) -> str:
    p, notes = MN.pad(user, pid), MN.pad_notes(user, pid)
    pending, rest = [r for r in notes if r.get("status") == "pending"], [r for r in notes if r.get("status") != "pending"]
    history = "".join(_proposal_html(pid, r) if r["kind"] == "proposal" else f"""<div class="list-row"><span class="status-label">{_when(r["t"])} {UI.escape(r["author"])}:</span> {UI.escape(r["text"])}</div>""" for r in reversed(rest))
    close = f"""hx-post="/im/in" hx-target="body" hx-swap="none" hx-vals='{json.dumps({"type": "muninn_pads_close_tab", "lvl": 1, "id": pid})}'""" if p["status"] == "open" else _post("pad_set", slug=pid, status="open")
    note_form = f"""<form class="node-form" {_post("pad_note", slug=pid)}><textarea name="text" class="cm-input" rows="2" placeholder="Context for the assistant: what a block is for, what must stay, where things belong" required></textarea><button type="submit" class="ui-btn">Add note</button></form>"""
    return f"""<div class="row"><input name="title" class="module-select grow" value="{UI.escape(p["title"])}" {_post("pad_set", slug=pid)} hx-trigger="change" hx-include="this">
                   <span id="mn-pad-status" class="status-label">saved {_when(p["updated"])}</span>
                   <button class="ui-btn" {close} title="{"Move to the pile - kept until its content is dealt with" if p["status"] == "open" else "Back to the open pads"}">{"Close" if p["status"] == "open" else "Reopen"}</button>
                   <button class="ui-btn" {_post("pad_delete", slug=pid)} hx-confirm="{"Delete this pad, its history and its notes? This cannot be undone." if p["text"].strip() else "Delete this empty pad?"}">Delete</button></div>
               <form id="mn-pad-form" class="node-form" {_post("pad_save", slug=pid)}>
                   <span id="mn-pad-ver"><input type="hidden" name="version" value="{p["version"]}"></span><input type="hidden" name="editor" value="{secrets.token_hex(4)}">
                   <textarea name="text" class="cm-input mn-pad-text" placeholder="Anything. No format, no order - it stays here until it has been moved somewhere or you know it is not needed." {_post("pad_save", slug=pid)} hx-trigger="keyup changed delay:1500ms, blur" hx-include="#mn-pad-form" data-keep-url="/im/in" data-keep-vals='{json.dumps({"type": "muninn_pad_save", "lvl": 1, "slug": pid})}' data-keep-status="mn-pad-status" data-keep-tab="tab-{pid}">{UI.escape(p["text"])}</textarea>
                   <button type="submit" class="ui-btn">Save</button></form>
               {_section(f"Proposals ({len(pending)})", "".join(_proposal_html(pid, r) for r in pending) or _empty("None waiting. The assistant will suggest moving blocks to a thread or the wiki, deleting or reducing them here; nothing changes until you accept."), bool(pending))}
               {_section("Notes", note_form + history, bool(notes))}"""

def _pile_html(user: str) -> str:
    rows = "".join(f"""<div class="row"><button class="chip" {_post("view", view="pad", slug=m["id"])}>{UI.escape(m["title"])}</button><span class="status-label">{_when(m["updated"])}</span><button class="cm-qbtn" title="Reopen" {_post("pad_set", slug=m["id"], status="open")}>&#x21A9;</button></div>""" for m in MN.pads(user, "closed"))
    return f"""<div class="row"><span class="form-title">Pile</span><span class="status-label">Closed pads. Kept until their content has been moved or is known not to be needed.</span></div>{rows or _empty("The pile is empty.")}"""

# --- Reduce: what can leave the live notes (destroy, or seal into the encrypted archive) ---

def _many(payload: dict, key: str) -> list: return [v for v in ([payload[key]] if isinstance(payload.get(key), str) else payload.get(key, [])) if v]   # a checkbox group arrives as a list, or a string when one is checked

def _reduce_html(user: str, msg: str = "", opened: dict = None) -> str:
    st = MN._ustate(user)
    grace, moot = int(st.get("grace_days", 30)), int(st.get("moot_days", 90))
    r = MN.reduction(user, grace, moot)
    cap = lambda e, on, why="": f"""<label class="list-row row"><input type="checkbox" name="e" value="{e["id"]}"{" checked" if on else ""}><span class="status-label">{_when(e["t"])}</span><span class="grow">{UI.escape(e["text"][:160])}</span><span class="status-label">{UI.escape(why)}</span>{"".join(_chip(t) for t in e["threads"])}</label>"""
    pad = lambda m: f"""<label class="list-row row"><input type="checkbox" name="p" value="{m["id"]}"{" checked" if m["empty"] else ""}><span class="status-label">{_when(m["updated"])}</span><span class="grow">{UI.escape(m["title"])}</span><span class="status-label">{"empty" if m["empty"] else "untouched"}</span></label>"""
    tick = """<button type="button" class="chip" title="Tick every item in this list" onclick="event.preventDefault(); this.closest('details').querySelectorAll('input[type=checkbox]').forEach(function(c){ c.checked = true; })">all</button>"""
    prop = lambda e: f"""<div class="list-row row"><label class="row grow"><input type="checkbox" name="e" value="{e["id"]}"><span class="status-label">{_when(e["t"])}</span><span class="grow">{UI.escape(e["text"][:160])}</span><span class="status-label">Huginn: {UI.escape(e["why"])}</span>{"".join(_chip(t) for t in e["threads"])}</label>
                             <input id="mn-keep-{e["mid"]}" name="reply_{e["mid"]}" class="module-select" placeholder="why it stays (optional)" style="width:10rem"><button type="button" class="ui-btn" {_post("moot_keep", mid=e["mid"])} hx-include="#mn-keep-{e["mid"]}" title="It still matters - Huginn reads your reason on later passes">Keep</button></div>"""
    lists = f"""{_section(f"Huginn: no longer needed? ({len(r['proposed'])}) {tick}", "".join(prop(e) for e in r["proposed"]) or _empty("None waiting."), bool(r["proposed"]))}
                {_section(f"In a card for {grace}+ days ({len(r['absorbed'])}) {tick}", "".join(cap(e, False) for e in r["absorbed"]) or _empty("None."), bool(r["absorbed"]))}
                {_section(f"Probably moot - {moot}+ days old ({len(r['moot'])}), your call {tick}", "".join(cap(e, False, e["why"]) for e in r["moot"]) or _empty("None."), bool(r["moot"]))}
                {_section(f"Pile pads ({len(r['pads'])}) {tick}", "".join(pad(m) for m in r["pads"]) or _empty("None."), bool(r["pads"]))}"""   # nothing that has content starts ticked: leaving is always your choice
    destroy = """<button type="button" class="ui-btn" style="color:#ff5f5f" hx-post="/im/in" hx-target="body" hx-swap="none" hx-include="#mn-reduce" hx-vals='{"type": "muninn_reduce_destroy", "lvl": 1}' hx-confirm="Delete the checked items for good? They leave the live notes now (backups keep copies until they rotate out); only their ids stay in the journal.">Destroy checked</button>"""
    seal = """<input type="password" name="pw" class="module-select" placeholder="passphrase" autocomplete="new-password"><input type="password" name="pw2" class="module-select" placeholder="again" autocomplete="new-password">
               <button type="button" class="ui-btn" hx-post="/im/in" hx-target="body" hx-swap="none" hx-include="#mn-reduce" hx-vals='{"type": "muninn_reduce_seal", "lvl": 1}'>Seal checked</button>"""
    arch = "".join(f"""<div class="list-row row"><span class="grow">{_when(a["sealed"])}</span><span class="status-label">{a["count"]["captures"]} capture(s), {a["count"]["pads"]} pad(s)</span>
                       <form class="row" {_post("archive", name=a["name"])} hx-include="this"><input type="password" name="pw" class="module-select" placeholder="passphrase" autocomplete="current-password"><button type="submit" name="act" value="open" class="ui-btn">Open</button><button type="submit" name="act" value="restore" class="ui-btn">Restore</button></form></div>""" for a in MN.archives(user))
    shown = ""
    if opened: shown = f"""<div class="info-bar">Archive {UI.escape(opened["name"])} (read here only - nothing restored):</div>{"".join(_entry_html({**c, "done": [], "pinned": [], "due": {}, "parked": {}, "absorbed": "", "commands": c.get("commands", []), "cmd": {}}) for c in opened["captures"])}{"".join(f'<div class="list-row"><b>{UI.escape(p["title"])}</b><div class="pre-wrap mono">{UI.escape(p["text"])}</div></div>' for p in opened["pads"])}"""
    return f"""<div class="row"><span class="form-title">Reduce</span><span class="status-label">{msg}</span></div>
               <div class="status-label">What can leave the live notes. Nothing goes until you choose it. <b>Destroy</b>: gone (the journal keeps only ids). <b>Seal</b>: encrypted into one archive with your passphrase - it is never stored, so if it is lost nobody can open the archive, this server included.</div>
               <form class="row" {_post("reduce_set")} hx-include="this"><span class="status-label">absorbed for</span><input type="number" name="grace_days" value="{grace}" min="0" class="module-select"><span class="status-label">days; moot after</span><input type="number" name="moot_days" value="{moot}" min="1" class="module-select"><span class="status-label">days</span><button type="submit" class="ui-btn">Apply</button></form>
               <form id="mn-reduce" class="node-form" onsubmit="return false">{lists}<div class="row">{destroy}<span class="grow"></span>{seal}</div></form>
               {_section(f"Sealed archives ({len(MN.archives(user))})", (arch or _empty("None yet.")) + shown, bool(arch))}"""

async def _h_absorb(request, payload, imr):
    MN.edit(_user(request), payload["entry"], "absorb" if str(payload.get("on")) == "1" else "unabsorb")
    return await _show(request, imr, await _view(request))

async def _h_moot_keep(request, payload, imr):
    MN.keep_moot(_user(request), payload["mid"], payload.get(f"""reply_{payload["mid"]}""", ""))   # one reply box per row; the Reduce form sends all of them
    return await _show(request, imr, {"view": "reduce"})

async def _h_reduce_set(request, payload, imr):
    MN._ustate_put(_user(request), grace_days=max(0, int(payload.get("grace_days") or 30)), moot_days=max(1, int(payload.get("moot_days") or 90)))
    return await _show(request, imr, {"view": "reduce"})

async def _h_reduce_destroy(request, payload, imr):
    n = MN.destroy(_user(request), _many(payload, "e"), _many(payload, "p"))
    return await _show(request, imr, {"view": "reduce"}, _reduce_html(_user(request), f"""Destroyed {n["captures"]} capture(s) and {n["pads"]} pad(s)."""))

async def _h_reduce_seal(request, payload, imr):
    user, es, ps = _user(request), _many(payload, "e"), _many(payload, "p")
    if payload.get("pw", "") != payload.get("pw2", ""): msg = "The two passphrases differ - nothing sealed."
    elif not es and not ps: msg = "Nothing checked."
    else:
        try: msg = f"""Sealed {len(es)} capture(s) and {len(ps)} pad(s) into archive {MN.seal(user, payload.get("pw", ""), es, ps)}. Keep the passphrase somewhere safe."""
        except ValueError as e: msg = f"Not sealed - {e}."
    return await _show(request, imr, {"view": "reduce"}, _reduce_html(user, msg))

async def _h_archive(request, payload, imr):
    """Open (read it here, nothing changes) or restore (back into the live notes; the archive file goes) one sealed archive with its passphrase."""
    user, name = _user(request), payload.get("name", "")
    try:
        if payload.get("act") == "restore":
            n = MN.restore(user, name, payload.get("pw", ""))
            html = _reduce_html(user, f"""Restored {n["captures"]} capture(s) and {n["pads"]} pad(s).""")
        else: html = _reduce_html(user, "", {"name": name, **MN.unseal(user, name, payload.get("pw", ""))})
    except ValueError as e: html = _reduce_html(user, f"Not opened - {e}.")
    return await _show(request, imr, {"view": "reduce"}, html)

# --- Huginn ---

def _due_elsewhere(user: str) -> list:
    """What other modules say is due (any service offering due_items - e.g. Finance's unpaid bills), so Today shows it beside the next steps."""
    return sorted((i for svc in ENV["services"].values() if "due_items" in svc for i in svc["due_items"](user, 14)), key=lambda i: i["date"])

def _money_html(items: list, d0: str) -> str:
    return "".join(f"""<div class="row"><b>{datetime.fromisoformat(i["date"]).strftime("%a %m-%d")}</b>{' <span class="status-label">(was due - not marked paid)</span>' if i["date"] < d0 else ""}<span>{UI.escape(i["label"])}</span><span class="status-label">{-i["amount"]:,.2f} &#xB7; {UI.escape(i["where"])}</span><button class="chip" hx-post="/im/in" hx-target="body" hx-swap="none" hx-vals='{json.dumps({"type": "_portal_open_tab", "path": i["url"], "label": i["app"], "id": i["app"].lower(), "lvl": 0})}'>{UI.escape(i["app"])}</button></div>""" for i in items)

def _surveys_html(rows: list) -> str:
    """Surveys Huginn queued: thread, why, the run's state; the report itself arrives on the thread as a capture."""
    return "".join(f"""<div class="row">{_chip(r["slug"])}<span class="status-label">{_when(r["t"])}</span><span class="grow">{UI.escape(r.get("why", ""))}</span><span class="status-label">{UI.escape(r["run"]["status"]) if r["run"] else "run deleted"}</span></div>""" for r in rows)

def _survey_html(user: str, cfg: dict, admin: bool) -> str:
    """Survey switches: the admin's choice of agent settings, this login's opt-in, and what was queued this week."""
    svc = HG.agent()
    if not svc: return _empty("The Agent tool (AI Tools > Agent) is not installed - surveys need it.")
    chosen = f"""Agent settings: {UI.escape(svc["settings_summary"](cfg["survey_settings"]))} (always read-only)""" if cfg["survey_settings"] else "Agent settings: not chosen yet - surveys are off."
    pick = f"""<button class="ui-btn" {_post("survey_settings")} title="Copies the settings of the newest run you queued in Agent (connection, model, space, files, knowledge)">Use my last Agent run's settings</button>""" if admin else ""
    me = user in cfg["survey_users"]
    switch = f"""<button class="ui-btn" {_post("survey_me", on=0 if me else 1)}>{"Stop surveys on my threads" if me else "Allow surveys on my quiet threads"}</button>"""
    cap = f"""up to {cfg["survey_cap"]} a day, threads quiet for {cfg["stall_days"]} days""" if int(cfg["survey_cap"]) else "the daily cap is 0 (Settings) - surveys are off"
    return f"""<div class="status-label">For threads that went quiet, Huginn can queue a read-only agent run that looks into them; its report comes back to the thread as a capture. Every run is listed in Agent (by huginn), where it can be stopped.</div>
               <div class="row"><span class="status-label grow">{chosen}</span>{pick}</div><div class="row">{switch}<span class="status-label">{cap}</span></div>{_surveys_html(HG.surveys(user, 7)) or _empty("No surveys in the last 7 days.")}"""

def _hg_settings(cfg: dict, msg: str = "") -> str:
    return f"""<form class="node-form" {_post("huginn_save")}><div id="mn-hg-fields" class="settings-compact">{HG.fields().render(cfg, name_prefix="hg_")}</div><span id="mn-hg-msg">{msg}</span>
               <div class="row"><span class="status-label grow">A pass uses: {UI.escape(HG.describe())}</span><button type="submit" class="ui-btn">Save settings</button></div></form>"""

async def _huginn_html(request) -> str:
    user, cfg, admin = _user(request), HG.config(), request.state.user.role == "admin"
    settings = _hg_settings(cfg) if admin else f"""<div class="status-label">A pass uses: {UI.escape(HG.describe())} - schedule {"on" if cfg["enabled"] else "off"} (set by an admin)</div>"""
    me = user in cfg["users"]
    log = "".join(f"""<div class="list-row status-label">{UI.escape(r["t"][5:16].replace("T", " "))} <b>{UI.escape(r["event"])}</b> {UI.escape(", ".join(f"{k}={v}" for k, v in r.items() if k not in ("t", "event", "raw")))}{f'<pre class="quote-block">{UI.escape(r["raw"])}</pre>' if r.get("raw") else ""}</div>""" for r in HG.recent_log(user))
    charter = f"""<form class="node-form" {_post("charter_save")}><textarea name="text" class="cm-input" rows="12">{UI.escape(HG.charter(user))}</textarea><div class="row"><span class="grow"></span><button type="submit" class="ui-btn">Save charter</button></div></form>"""
    return f"""<div class="row"><span class="form-title">Huginn</span><span class="status-label">Proposes only - every change waits for your answer.{" A pass is running now." if HG.running(user) else ""}</span></div>
               <div class="row"><button class="ui-btn" {_post("huginn_me", on=0 if me else 1)}>{"Stop working on my notes" if me else "Work on my notes"}</button><button class="ui-btn" {_post("huginn_run")}>Run a pass now</button></div>
               {_section("Settings", settings, admin and not cfg["enabled"])}
               {_section("Surveys", _survey_html(user, cfg, admin), False)}
               {_section("Charter (read at every pass - edit to change what it does)", charter, False)}
               {_section("Log", log or _empty("No passes yet."))}"""

async def _main_html(request, v: dict) -> str:
    user = _user(request)
    if v.get("view") == "pad" and v.get("slug") in {m["id"] for m in MN.pads(user, "closed")}: return _pad_body(user, v["slug"])
    if v.get("view") == "pad": return await _pads_html(request, v.get("slug", ""))
    if v.get("view") == "huginn": return await _huginn_html(request)
    if v.get("view") == "reduce": return _reduce_html(user)
    if v.get("view") == "recent": return _recent_html(user)
    if v.get("view") == "pile": return _pile_html(user)
    if v.get("view") == "thread" and v.get("slug"): return _thread_html(user, v["slug"])
    if v.get("view") == "search" and v.get("q"): return _search_html(user, v["q"])
    if v.get("view") == "import": return _import_html()
    return await _today_html(request)

def _side_html(user: str, active: str = "") -> str:
    ts, open_pads, piled = MN.threads(user), MN.pads(user, "open"), len(MN.pads(user, "closed"))
    pad_rows = "".join(f"""<div class="side-list-item{" active" if m["id"] == active else ""}" {_post("view", view="pad", slug=m["id"])}><span class="side-list-item-title">{UI.escape(m["title"])}</span><span class="status-label">{_when(m["updated"])}</span></div>""" for m in open_pads)
    rows = "".join(f"""<div class="side-list-item{" active" if t["slug"] == active else ""}" {_post("view", view="thread", slug=t["slug"])}><span class="side-list-item-title">{UI.escape(t["title"])}</span><span class="status-label">{t["count"]}{f' &#xB7; {t["open"]} next' if t["open"] else ""}</span></div>""" for t in ts)
    return f"""<form {_post("view", view="search")}><input name="q" class="module-select" placeholder="Search captures and cards" required></form>
               <div class="row"><button class="ui-btn" {_post("view", view="today")}>Today</button><button class="ui-btn" {_post("view", view="recent")}>Recent</button><button class="ui-btn" {_post("view", view="import")}>Import</button><button class="ui-btn" {_post("view", view="huginn")}>Huginn</button><button class="ui-btn" {_post("view", view="reduce")} title="What can leave the live notes: destroy or seal into an encrypted archive">Reduce</button></div>
               <div class="list-panel-hd"><span class="list-panel-title">Pads ({len(open_pads)})</span><button class="btn-icon" title="New pad" {_post("pads_open_tab")}>+</button></div>{pad_rows}
               <div class="row"><button class="ui-btn" {_post("view", view="pile")}>Pile ({piled})</button></div>
               <div class="list-panel-hd"><span class="list-panel-title">Threads ({len(ts)})</span></div>{rows or _empty("Threads appear when a capture has a #tag.")}
               <datalist id="mn-thread-list">{"".join(f'<option value="{t["slug"]}">' for t in ts)}</datalist>"""

async def _view(request) -> dict: return await ENV["get_state"](request, scope="user", namespace="muninn", key="view") or {"view": "today"}

async def _show(request, imr, v: dict, main: str = None):
    """Stores the view, renders it into the main area and refreshes the thread list."""
    HG.ensure_worker()
    await ENV["set_state"](request, v, scope="user", namespace="muninn", key="view")
    imr.oob(main if main is not None else await _main_html(request, v), "mn-main")
    return imr.oob(_side_html(_user(request), v.get("slug", "") if v.get("view") in ("thread", "pad") else ""), "mn-side")

# --- Intent handlers ---

async def _h_view(request, payload, imr): return await _show(request, imr, {k: payload[k] for k in ("view", "slug", "q") if payload.get(k)})

async def _h_capture(request, payload, imr):
    MN.capture(_user(request), payload.get("text", ""), source="muninn", ctx=await MN.context(request), threads=[payload["thread"]] if payload.get("thread") else None)
    return await _show(request, imr, await _view(request))

async def _h_link(request, payload, imr):
    MN.edit(_user(request), payload["entry"], "thread_add", thread=payload["thread"])
    return await _show(request, imr, await _view(request))

async def _h_next(request, payload, imr):
    MN.edit(_user(request), payload["entry"], "next_done" if str(payload.get("done")) == "1" else "next_open", index=int(payload["index"]))
    return await _show(request, imr, await _view(request))

async def _h_pin(request, payload, imr):
    MN.edit(_user(request), payload["entry"], "next_pin" if str(payload.get("pin")) == "1" else "next_unpin", index=int(payload["index"]))
    return await _show(request, imr, await _view(request))

async def _h_next_set(request, payload, imr):
    """Due date (blank clears one set here), park until a date (blank = a week), unpark, drop (closed as not needed), or rate how much it matters (level_high / level_some / level_low)."""
    user, eid, i, op, d = _user(request), payload["entry"], int(payload["index"]), payload.get("op", ""), payload.get("date", "")
    if op == "due": MN.edit(user, eid, "next_due", index=i, due=d)
    elif op == "park": MN.edit(user, eid, "next_park", index=i, until=d or (datetime.now() + timedelta(days=7)).date().isoformat())
    elif op == "unpark": MN.edit(user, eid, "next_unpark", index=i)
    elif op == "drop": MN.edit(user, eid, "next_done", index=i, dropped=True)
    elif op.startswith("level_"): MN.signal_set(user, f"{eid}:{i}", op[6:])
    return await _show(request, imr, await _view(request))

async def _h_suggest(request, payload, imr):
    MN.answer_suggestion(_user(request), payload["sid"], str(payload.get("accept")) == "1", thread=payload.get("thread"), next_step=payload.get("next"))
    return await _show(request, imr, await _view(request))

async def _h_cmd(request, payload, imr):
    """A command line on Today: run it as the box shows it, check what the edited line would do (only that row changes), or dismiss it."""
    user, eid, i, op, line = _user(request), payload["entry"], int(payload["index"]), payload.get("op", "run"), payload.get("line", "")
    e = next(x for x in MN.entries(user) if x["id"] == eid)
    if op == "check": return imr.oob(_cmd_row(e, i, line, await MN.command_preview(request, line, e)), f"""mn-cmd-{eid}-{i}""")
    if op == "dismiss": MN.dismiss_command(user, eid, i)
    r = await MN.run_command(request, eid, i, line) if op == "run" else {"ok": True, "result": ""}
    if not r["ok"]: return imr.oob(_cmd_row(e, i, line, {"error": r["result"]}), f"""mn-cmd-{eid}-{i}""")
    note = f"""<div class="info-bar">&#x2713; {UI.escape(line)} &#x2192; {UI.escape(r["result"])}</div>""" if r["result"] else ""
    return await _show(request, imr, {"view": "today"}, await _today_html(request, note))

async def _h_huginn_save(request, payload, imr):
    if request.state.user.role != "admin": return await _show(request, imr, {"view": "huginn"})
    try: HG.save_config(**HG.fields().parse(payload, name_prefix="hg_"))
    except BI.SettingsError as e: return imr.oob(f"""<span class="err-box">{UI.escape(str(e))}</span>""", "mn-hg-msg")
    return await _show(request, imr, {"view": "huginn"})

async def _h_huginn_refresh(request, payload, imr):
    """The connection changed: the fields again with what was typed, Model now listing that connection's models (cleared - the old one belongs to the old connection)."""
    try: v = HG.fields().parse(payload, name_prefix="hg_")
    except BI.SettingsError as e: return imr.oob(f"""<span class="err-box">{UI.escape(str(e))}</span>""", "mn-hg-msg")
    imr.oob("", "mn-hg-msg")
    return imr.oob(HG.fields().render({**v, "model": ""}, name_prefix="hg_"), "mn-hg-fields")

async def _h_huginn_me(request, payload, imr):
    users = [u for u in HG.config()["users"] if u != _user(request)] + ([_user(request)] if str(payload.get("on")) == "1" else [])
    HG.save_config(users=users)
    return await _show(request, imr, {"view": "huginn"})

async def _h_charter_save(request, payload, imr):
    HG.save_charter(_user(request), payload.get("text", ""))
    return await _show(request, imr, {"view": "huginn"})

async def _h_survey_me(request, payload, imr):
    HG.save_config(survey_users=[u for u in HG.config()["survey_users"] if u != _user(request)] + ([_user(request)] if str(payload.get("on")) == "1" else []))
    return await _show(request, imr, {"view": "huginn"})

async def _h_survey_settings(request, payload, imr):
    """Admin: the agent settings surveys use are the settings of the admin's newest Agent run."""
    s = HG.agent()["last_settings"](_user(request)) if request.state.user.role == "admin" else None
    if s: HG.save_config(survey_settings={k: v for k, v in s.items() if k not in ("muninn_thread", "again_of")})
    await _show(request, imr, {"view": "huginn"})
    return imr.oob("" if s else "No run of yours in Agent yet - queue one there with the settings surveys should use.", "mn-hg-msg") if request.state.user.role == "admin" else imr

async def _h_huginn_run(request, payload, imr):
    HG.run_now(_user(request))
    return await _show(request, imr, {"view": "huginn"})

async def _h_card_edit(request, payload, imr): return await _show(request, imr, {"view": "thread", "slug": payload["slug"]}, _thread_html(_user(request), payload["slug"], editing=True, prop=payload.get("prop", "")))

async def _h_card_save(request, payload, imr):
    """Saves the card. Editing Huginn's proposal first: saving accepts the proposal as edited (stale if the card changed meanwhile - then nothing is overwritten)."""
    user, s = _user(request), payload["slug"]
    if payload.get("prop"):
        status = MN.card_answer(user, payload["prop"], True, body=payload.get("body", ""), title=payload.get("title", ""))
        if status == "stale": return await _show(request, imr, {"view": "thread", "slug": s}, f"""<div class="err-box">The card changed after Huginn proposed this - nothing was overwritten. Your edited text is below; copy what you need.</div><pre class="quote-block">{UI.escape(payload.get("body", ""))}</pre>{_thread_html(user, s)}""")
    else: MN.save_page(user, s, payload.get("body", ""), title=payload.get("title", ""))
    return await _show(request, imr, {"view": "thread", "slug": s})

async def _h_card_answer(request, payload, imr):
    status = MN.card_answer(_user(request), payload["prop"], str(payload.get("accept")) == "1", reply=payload.get("reply", ""))
    note = '<div class="err-box">The card changed after Huginn proposed this - nothing was overwritten; the proposal is marked stale.</div>' if status == "stale" else ""
    return await _show(request, imr, {"view": "thread", "slug": payload["slug"]}, note + _thread_html(_user(request), payload["slug"]))

async def _h_import(request, payload, imr): return await _show(request, imr, {"view": "import"}, _import_html(MN.import_markdown(_user(request), payload.get("text", ""))))

async def _h_pad_save(request, payload, imr):
    """Saves as you type, without re-rendering the editor. If the pad changed elsewhere, what was typed is already safe in a new pad - the editor moves to that copy so later typing cannot overwrite the other version."""
    r = MN.pad_save(_user(request), payload["slug"], payload.get("text", ""), int(payload.get("version") or 0), editor=payload.get("editor", ""))
    if r["conflict"]:
        await _show(request, imr, {"view": "pad", "slug": r["conflict"]["id"]})
        return imr.oob("This pad changed on another device or by an accepted proposal - your text continues in this copy", "mn-pad-status")
    imr.oob(f"""<input type="hidden" name="version" value="{r["meta"]["version"]}">""", "mn-pad-ver")
    return imr.oob(f"""saved {_when(r["meta"]["updated"])}""", "mn-pad-status")

async def _h_pad_set(request, payload, imr):
    MN.pad_set(_user(request), payload["slug"], title=payload.get("title", ""), status=payload.get("status", ""))
    return await _show(request, imr, {"view": "pad", "slug": payload["slug"]})

async def _h_pad_delete(request, payload, imr):
    MN.pad_delete(_user(request), payload["slug"])
    return await _show(request, imr, {"view": "pad"})

async def _h_pad_note(request, payload, imr):
    MN.pad_note(_user(request), payload["slug"], payload.get("text", ""))
    return await _show(request, imr, {"view": "pad", "slug": payload["slug"]})

def _wiki_deliver(request):
    """Adds an accepted wiki block at the end of its page, as this login (the wiki's own edit rules apply); the pad's notes keep where it went."""
    def deliver(p):
        if "wiki" not in ENV["services"]: raise ValueError("the Wiki module is not installed")
        ENV["services"]["wiki"]["append"](request, p["target"], p["block"])
    return deliver

async def _h_pad_answer(request, payload, imr):
    try: status = MN.pad_answer(_user(request), payload["slug"], payload["prop"], str(payload.get("accept")) == "1", payload.get("reply", ""), deliver=_wiki_deliver(request))
    except (ValueError, PermissionError) as e: return imr.oob(f"Nothing changed - {UI.escape(str(e))}", "mn-pad-status")
    await _show(request, imr, {"view": "pad", "slug": payload["slug"]})
    return imr.oob({"stale": "The block is no longer in the pad exactly as proposed - nothing changed; the proposal is marked stale", "accepted": "Accepted - the pad is updated", "declined": "Declined - your reply is read at the next pass"}.get(status, status), "mn-pad-status")

# --- Route ---

@router.get("/")
async def root(request: Request):
    v, user = await _view(request), _user(request)
    HG.ensure_worker()
    if "wiki" in ENV["services"]: HG.save_places(user, ENV["services"]["wiki"]["pages"](request))   # what Huginn may suggest as a place in the wiki for this login
    side = UI.toolbar(side="left", content=f'<div id="mn-side" class="scroll-pane">{_side_html(user, v.get("slug", "") if v.get("view") in ("thread", "pad") else "")}</div>', size="15rem", overlay=False, start_open=True, resizable=True, id="mn-left", nesting_level=1, phone_closed=True, close_on_select=True)   # phones: the view first; the list closes after a choice
    return ENV["templates"].TemplateResponse(name="base.html", request=request, context={"request": request, "user": request.state.user, "nesting_level": 1, "shell_id": IM.branch_id, "toolbars": {"left": side}, "content": f'<div id="mn-main" class="scroll-pane">{await _main_html(request, v)}</div>{MN.KEEP_JS}', "extra_css": CSS + BI.MD_BLOCK_CSS})

CSS = """
.mn-l-next{color:var(--accent);}
.mn-l-question{color:#ffcc00;}
.mn-l-tried{opacity:.75;font-style:italic;}
.mn-l-command{color:#7fd1ff;font-family:var(--font-mono);}
.mn-link{display:inline-block;}
.mn-link>summary{cursor:pointer;list-style:none;}
.mn-link form{display:inline-flex;margin:0 0 0 .2rem;}
textarea.cm-input.mn-pad-text{min-height:55vh;max-height:none;font-family:var(--font-mono);font-variant-ligatures:none;font-feature-settings:"liga" 0,"calt" 0;resize:vertical;}
"""
