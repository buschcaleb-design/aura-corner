#!/usr/bin/env python3
"""
Aura Corner — web chat (v1, 2026-10-04)

A friendly chat window for AC with the damage calculator built in.
Rose-friendly: just open http://localhost:5000 in a browser.

SETUP:
  1. LM Studio → Developer tab → Start Server (http://localhost:1234)
  2. pip install flask openai requests
  3. python aura_corner_web.py
  4. Open http://localhost:5000
"""

import json
import os
import re
import time
import requests
from flask import Flask, request, jsonify, render_template_string
from openai import OpenAI

LMSTUDIO_URL = "http://localhost:1234/v1"
DAMAGE_API = "https://poke-companion.gigabytelife.net/api/damage-calc"

SYSTEM_PROMPT = """You are Aura Corner — AC for short. Caleb's Pokémon Champions battle coach. You're direct, warm, and you never bluff.

You have three tools:
- analyze_matchup: USE THIS FIRST for any battle question. Give it Caleb's Pokémon and the opponent's species. It checks all of Caleb's moves, picks the best, looks up the foe's real moveset, picks their best move, and returns both with damage ranges, HP, and worst-case turns. One call does it all.
- damage_calc: for follow-up questions about specific moves.
- get_moveset: to see an opponent's full moveset.

YOUR REPORT FORMAT:

1. OFFENSE — Call analyze_matchup. Report Caleb's best move, its damage range, and worst-case turns to KO.
2. DEFENSE — From the same analyze_matchup result. Report the foe's best move, its damage range, and worst-case turns until Caleb's Pokémon is KO'd.
3. DECISION — Attack or Switch, one line why.

Use Caleb's team movesets below for his Pokémon. Never invent a move for either side — if it's not in the moveset, it doesn't exist.
If a tool returns an error or no data, say so plainly and stop — never invent damage ranges, HP values, or turn counts to fill the gap.

CALEB'S TEAM (__TEAM_NAME__ — Singles 3v3, he picks 3 of 6):
HP formula: Base HP + StatPoints + 75. HP values below are exact.
__TEAM_LINES__

"""


def _team_lines():
    """Render the current team for the system prompt."""
    lines = []
    for i, sp in enumerate(TEAM_MOVES, 1):
        det = TEAM_DETAILS.get(sp, {})
        bits = [TEAM_NATURES.get(sp, "Hardy")]
        if det.get("evs"):
            bits.append(det["evs"])
        if det.get("item"):
            bits.append(det["item"])
        bits.append(f"{TEAM_HP.get(sp, '?')} HP")
        mv = " / ".join(m["name"] for m in TEAM_MOVES[sp])
        lines.append(f"{i}. {sp} ({', '.join(bits)}): {mv}")
    return "\n".join(lines)


def build_system_prompt():
    return SYSTEM_PROMPT.replace("__TEAM_NAME__", TEAM_NAME).replace("__TEAM_LINES__", _team_lines())


def _compute_hp(species, evs_text):
    """HP = base HP (PokeAPI) + HP EVs + 75. Returns None if the species
    isn't found (Champions-original forms) — then HP must be typed in."""
    m = re.search(r"(\d+)\s*hp", evs_text or "", re.IGNORECASE)
    hp_evs = int(m.group(1)) if m else 0
    slug = _canon(species).lower().replace(" ", "-")
    try:
        r = requests.get(f"https://pokeapi.co/api/v2/pokemon/{slug}", timeout=10)
        if r.status_code != 200:
            return None
        for s in r.json().get("stats", []):
            if s["stat"]["name"] == "hp":
                return s["base_stat"] + hp_evs + 75
    except Exception:
        return None
    return None

DAMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "damage_calc",
        "description": (
            "Calculate exact Pokémon damage via the Poké Companion calculator. "
            "Use for ANY damage question. Supports singles and doubles. "
            "Returns all 16 rolls, min/max, KO summary, effectiveness, STAB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "attacker_species": {"type": "string"},
                "attacker_level": {"type": "integer", "default": 50},
                "attacker_nature": {"type": "string"},
                "defender_species": {"type": "string"},
                "defender_level": {"type": "integer", "default": 50},
                "move_name": {"type": "string"},
                "move_power": {"type": "integer"},
                "move_type": {"type": "string"},
                "move_category": {"type": "string", "enum": ["physical", "special"]},
                "battle_format": {"type": "string", "enum": ["singles", "doubles"], "default": "singles",
                                  "description": "Use 'doubles' for double battles"},
                "defender2_species": {"type": "string",
                                     "description": "Second opposing Pokémon (doubles only)"},
                "ally_species": {"type": "string",
                                 "description": "Caleb's other active Pokémon (doubles only). Pass it so spread moves can be checked against your own ally."},
                "target": {"type": "string", "enum": ["foe1", "foe2", "both-foes", "ally"],
                           "description": "Which target(s) the move hits (doubles only). Spread moves hitting both foes get ×0.75. Use 'ally' to check friendly fire."},
            },
            "required": ["attacker_species", "defender_species", "move_name", "move_power", "move_type", "move_category"],
        },
    },
}

MOVESET_TOOL = {
    "type": "function",
    "function": {
        "name": "get_moveset",
        "description": (
            "Look up the most common competitive moveset for a species, from tournament-winning-team data. "
            "ALWAYS call this before deciding what move an OPPONENT would use — never invent a moveset. "
            "Returns moves, nature, item, ability, EVs (null = unknown, not zero). "
            "Unknown species return an error with a coveredSpecies list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "species": {"type": "string", "description": "The opponent's Pokemon species"},
            },
            "required": ["species"],
        },
    },
}

MOVESET_API = "https://pokemon-companion-pi.vercel.app/api/moveset"

# Amanda's Poké Companion team-code API. GET {TEAM_API_BASE}/{code} returns the
# team as JSON (see TEAM_CODE_CONTRACT below). Built against the proposed
# contract — wire-up is just confirming her endpoint matches.
TEAM_API_BASE = "https://poke-companion.gigabytelife.net/api/teams"
TEAM_CODE_CONTRACT = """
GET {base}/{code} -> 200 JSON:
{
  "code": "ABC123",
  "name": "My Team",
  "pokemon": [
    {"species": "Garchomp", "level": 50, "nature": "Jolly",
     "evs": "32 Atk / 4 SpD / 32 Spe", "item": "Rocky Helmet",
     "ability": "Rough Skin", "hp": 183,
     "moves": ["Earthquake", "Dragon Claw", "Stone Edge", "Swords Dance"]}
  ]
}
Notes: "pokemon" may also be called "team" or "members". Per-mon "species" may
be "name". "hp" is optional (AC computes base HP + HP EVs + 75 when missing).
"moves" are plain name strings; AC resolves power/type/category itself.
"""


def call_moveset_api(args: dict) -> str:
    try:
        r = requests.get(MOVESET_API, params={"species": args["species"]}, timeout=15)
        d = r.json()
        if r.status_code != 200 or d.get("error"):
            return json.dumps({"error": d.get("error", "unknown error"),
                               "covered_species": [s["name"] for s in d.get("coveredSpecies", [])]})
        sets = d.get("sets", [])
        out = []
        for s in sets[:2]:
            out.append({"moves": s.get("moves"), "nature": s.get("nature"),
                        "item": s.get("item"), "ability": s.get("ability"),
                        "evs": s.get("evs"),
                        "sources": [x.get("event", "") + " (" + x.get("player", "") + ")" for x in s.get("sources", [])]})
        return json.dumps({"species": d.get("species"), "sets": out,
                           "note": "null fields are unknown, not zero"})
    except Exception as e:
        return json.dumps({"error": str(e)})


PAGE = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aura Corner 🥊</title>
<style>
  * { box-sizing: border-box; font-family: system-ui, sans-serif; }
  body { margin: 0; background: #1a1a2e; color: #eee; display: flex; flex-direction: column; height: 100vh; }
  header { background: #16213e; padding: 14px 20px; font-size: 20px; font-weight: bold; }
  header span { font-weight: normal; font-size: 13px; color: #aaa; display: block; }
  #chat { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 10px; }
  .msg { max-width: 75%; padding: 10px 14px; border-radius: 14px; line-height: 1.45; white-space: pre-wrap; }
  .user { align-self: flex-end; background: #0f3460; }
  .ac { align-self: flex-start; background: #2a2a4a; }
  .tool { align-self: flex-start; background: #1e3a2a; color: #9fd; font-size: 13px; font-style: italic; }
  #bar { display: flex; padding: 12px; gap: 8px; background: #16213e; }
  #in { flex: 1; padding: 12px; border-radius: 10px; border: none; font-size: 16px; }
  #send { padding: 12px 22px; border-radius: 10px; border: none; background: #e94560; color: white; font-size: 16px; cursor: pointer; }
  #send:disabled { opacity: .5; }
</style></head>
<body>
<header>🥊 Aura Corner <span>AC is in your corner — ask about battles, damage, or strategy</span> <span id="modelname" style="font-size:12px;opacity:.7"></span> <span style="margin-left:auto"></span><a href="/team" style="background:#0f3460;color:#fff;border-radius:8px;padding:6px 12px;font-size:14px;text-decoration:none;">Build your team</a><button id="shutdown" title="Stop the AC server (use before starting a new copy)" style="margin-left:8px;background:#3a3a5a;color:#fff;border:none;border-radius:8px;padding:6px 12px;cursor:pointer;font-size:14px;">⏻</button></header>
<div id="chat"></div>
<div id="bar"><input id="in" placeholder="Ask AC anything..." autocomplete="off"><button id="send">Send</button></div>
<script>
const chat = document.getElementById('chat'), inp = document.getElementById('in'), btn = document.getElementById('send');
function add(text, cls) {
  const d = document.createElement('div'); d.className = 'msg ' + cls; d.textContent = text;
  chat.appendChild(d); chat.scrollTop = chat.scrollHeight; return d;
}
add("Hey! I'm AC — Aura Corner. 🥊 Ask me about your battles, damage calcs, or what to do next!", 'ac');
async function send() {
  const t = inp.value.trim(); if (!t) return;
  inp.value = ''; btn.disabled = true;
  add(t, 'user');
  const thinking = add('AC is thinking...', 'ac');
  try {
    const r = await fetch('/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: t})});
    const j = await r.json();
    thinking.remove();
    if (j.model) document.getElementById('modelname').textContent = '· ' + j.model;
    (j.tools || []).forEach(x => add('🧮 ' + x, 'tool'));
    add(j.reply, 'ac');
  } catch (e) { thinking.textContent = 'Oops — something went wrong. Is LM Studio running?'; }
  btn.disabled = false; inp.focus();
}
btn.onclick = send;
inp.onkeydown = e => { if (e.key === 'Enter') send(); };
document.getElementById('shutdown').onclick = async () => {
  if (!confirm('Stop the AC server? Restart team_builder.py to bring AC back.')) return;
  try { await fetch('/shutdown', {method: 'POST'}); } catch (e) {}
  chat.innerHTML = '';
  add('Server stopped. Run team_builder.py again to bring AC back.', 'ac');
};
</script></body></html>
"""


TEAM_PAGE = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aura Corner — Build your team</title>
<style>
  * { box-sizing: border-box; font-family: system-ui, sans-serif; }
  body { margin: 0; background: #1a1a2e; color: #eee; padding: 16px; }
  h2 { margin: 4px 0 12px; }
  a { color: #e94560; }
  #teamname { width: 100%; max-width: 420px; padding: 10px; border-radius: 8px; border: none; font-size: 16px; margin-bottom: 12px; }
  .mon { background: #2a2a4a; border-radius: 12px; padding: 12px; margin-bottom: 12px; }
  .mon h3 { margin: 0 0 8px; font-size: 16px; color: #e94560; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; }
  label { font-size: 12px; color: #aaa; display: block; margin-bottom: 2px; }
  input, select { width: 100%; padding: 8px; border-radius: 8px; border: none; font-size: 14px; }
  .moves { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px; }
  .evs { display: grid; grid-template-columns: repeat(auto-fill, minmax(80px, 1fr)); gap: 8px; margin-top: 8px; }
  .evs-head { grid-column: 1 / -1; display: flex; justify-content: space-between; align-items: center; }
  .evs-head span { font-size: 12px; color: #aaa; }
  #save { padding: 12px 28px; border-radius: 10px; border: none; background: #e94560; color: white; font-size: 16px; cursor: pointer; margin: 8px 0; }
  #status { margin: 8px 0; min-height: 20px; }
</style></head>
<body>
<h2>Build your team</h2>
<p><a href="/">← back to chat</a></p>
<input id="teamname" placeholder="Team name (e.g. Team 2)">
<div id="mons"></div>
<button id="save">Save team</button>
<div id="status"></div>
<script>
const NATURES = ["Hardy","Lonely","Brave","Adamant","Naughty","Bold","Docile","Relaxed","Impish","Lax","Timid","Hasty","Jolly","Naive","Modest","Mild","Quiet","Bashful","Rash","Calm","Gentle","Sassy","Careful","Quirky","Serious"];
const EV_STATS = [["ev_hp","HP"],["ev_atk","Atk"],["ev_def","Def"],["ev_spa","SpA"],["ev_spd","SpD"],["ev_spe","Spe"]];
const wrap = document.getElementById('mons');
function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }
function pretty(n) { return n.split('-').map(w => w.charAt(0).toUpperCase()+w.slice(1)).join(' '); }
function slugify(s) { return (s||'').toLowerCase().trim().replace(/[\s_]+/g,'-').replace(/[^a-z0-9-]/g,''); }
function evOptions(sel) {
  let o = '';
  if (sel > 32) o += `<option value="${sel}" selected>${sel} (old EV scale!)</option>`;
  for (let v = 0; v <= 32; v += 1) o += `<option value="${v}"${v===sel?' selected':''}>${v}</option>`;
  return o;
}
function parseEvs(s) {
  const out = {ev_hp:0,ev_atk:0,ev_def:0,ev_spa:0,ev_spd:0,ev_spe:0};
  const key = {hp:'ev_hp',atk:'ev_atk',def:'ev_def',defense:'ev_def',spa:'ev_spa',spatk:'ev_spa',spd:'ev_spd',spdef:'ev_spd',spe:'ev_spe',speed:'ev_spe'};
  (s||'').split('/').forEach(p => {
    const m = p.trim().match(/(\d+)\s*([a-zA-Z.\s]+)/);
    if (!m) return;
    const k = key[m[2].toLowerCase().replace(/[\s.]/g,'')];
    if (k) out[k] = parseInt(m[1],10);
  });
  return out;
}
function updateEvTotal(cardEl) {
  let t = 0;
  EV_STATS.forEach(([f]) => { t += parseInt(cardEl.querySelector(`[data-f="${f}"]`).value,10)||0; });
  const el = cardEl.querySelector('[data-f="evtotal"]');
  el.textContent = t;
  el.style.color = t > 66 ? '#f88' : '#aaa';
}
function candidates(slug) {
  const c = [slug];
  const base = slug.replace(/-?mega-?/g,'').replace(/-[xyz]$/,'').replace(/^-+|-+$/g,'');
  if (base && base !== slug) c.push(base);
  const hm = slug.match(/^(hisuian|galarian|alolan|paldean)-(.+)$/);
  if (hm) c.push(hm[2]+'-'+({hisuian:'hisui',galarian:'galar',alolan:'alola',paldean:'paldea'}[hm[1]]));
  return [...new Set(c)];
}
async function loadMonData(cardEl, species) {
  const slug = slugify(species);
  if (!slug) return;
  for (const cand of candidates(slug)) {
    try {
      const r = await fetch('https://pokeapi.co/api/v2/pokemon/'+cand);
      if (!r.ok) continue;
      const d = await r.json();
      const moves = [...new Set(d.moves.map(m => m.move.name))].sort().map(pretty);
      const abilities = d.abilities.map(a => pretty(a.ability.name));
      for (let k = 0; k < 4; k++) {
        const cur = cardEl.querySelector(`[data-f="move${k}"]`);
        const val = (cur.value||'').trim();
        const sel = document.createElement('select');
        sel.setAttribute('data-f','move'+k);
        let opts = '<option value="">-- pick a move --</option>';
        if (val && !moves.includes(val)) opts += `<option selected>${esc(val)}</option>`;
        opts += moves.map(mv => `<option${mv===val?' selected':''}>${mv}</option>`).join('');
        sel.innerHTML = opts;
        cur.replaceWith(sel);
      }
      const ab = cardEl.querySelector('[data-f="ability"]');
      const aval = (ab.value||'').trim();
      const asel = document.createElement('select');
      asel.setAttribute('data-f','ability');
      let aopts = '<option value="">-- pick an ability --</option>';
      if (aval && !abilities.includes(aval)) aopts += `<option selected>${esc(aval)}</option>`;
      aopts += abilities.map(a => `<option${a===aval?' selected':''}>${a}</option>`).join('');
      asel.innerHTML = aopts;
      ab.replaceWith(asel);
      return;
    } catch(e) {}
  }
}
function card(i, m) {
  m = m || {};
  const d = document.createElement('div'); d.className = 'mon';
  const nat = NATURES.map(n => `<option ${n===(m.nature||'Hardy')?'selected':''}>${n}</option>`).join('');
  const ev = parseEvs(m.evs);
  const evHtml = EV_STATS.map(([f,label]) =>
    `<div><label>${label}</label><select data-f="${f}">${evOptions(ev[f])}</select></div>`).join('');
  let moves = '';
  for (let k = 0; k < 4; k++) moves += `<div><label>Move ${k+1}</label><input data-f="move${k}" value="${esc((m.moves||[])[k])}" placeholder="type a species above"></div>`;
  d.innerHTML = `<h3>Pok\u00e9mon ${i+1}</h3>
    <div class="grid">
      <div><label>Species</label><input data-f="species" value="${esc(m.species)}" placeholder="e.g. Garchomp"></div>
      <div><label>Nature</label><select data-f="nature">${nat}</select></div>
      <div><label>Level</label><input data-f="level" type="number" value="${m.level||50}"></div>
      <div><label>Item (optional)</label><input data-f="item" value="${esc(m.item)}"></div>
      <div><label>Ability</label><input data-f="ability" value="${esc(m.ability)}" placeholder="type a species above"></div>
      <div><label>HP (blank = auto)</label><input data-f="hp" type="number" value="${m.hp||''}" placeholder="auto"></div>
    </div>
    <div class="evs"><div class="evs-head"><label>Stat Points</label><span>Total: <b data-f="evtotal">0</b> / 66</span></div>${evHtml}</div>
    <div class="moves">${moves}</div>`;
  d.querySelectorAll('[data-f^="ev_"]').forEach(s => s.addEventListener('change', () => updateEvTotal(d)));
  updateEvTotal(d);
  const sp = d.querySelector('[data-f="species"]');
  let t;
  sp.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => loadMonData(d, sp.value), 700); });
  if (m.species) loadMonData(d, m.species);
  return d;
}
async function init() {
  let t = {name:'', mons:[]};
  try { t = await (await fetch('/api/team')).json(); } catch(e) {}
  document.getElementById('teamname').value = t.name || '';
  for (let i = 0; i < 6; i++) wrap.appendChild(card(i, t.mons[i]));
}
document.getElementById('save').onclick = async () => {
  const st = document.getElementById('status'); st.textContent = 'Saving...'; st.style.color = '#eee';
  const mons = [...wrap.children].map(c => {
    const g = f => c.querySelector(`[data-f="${f}"]`).value.trim();
    const evParts = [];
    EV_STATS.forEach(([f,label]) => { const v = parseInt(g(f),10)||0; if (v > 0) evParts.push(v+' '+label); });
    return {species: g('species'), nature: g('nature'), level: g('level'), evs: evParts.join(' / '),
            item: g('item'), ability: g('ability'), hp: g('hp'),
            moves: [g('move0'), g('move1'), g('move2'), g('move3')]};
  });
  try {
    const r = await fetch('/save_team', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name: document.getElementById('teamname').value.trim(), mons})});
    const j = await r.json();
    st.textContent = j.ok ? `Saved! ${j.count} Pok\u00e9mon live. Go chat with AC.` : ('Error: ' + j.error);
    st.style.color = j.ok ? '#9fd' : '#f88';
  } catch(e) { st.textContent = 'Save failed \u2014 is the server running?'; st.style.color = '#f88'; }
};
init();

</script></body></html>
"""


def call_damage_api(args: dict) -> str:
    is_doubles = args.get("battle_format") == "doubles"
    payload = {
        "attacker": {"species": args["attacker_species"], "level": args.get("attacker_level", 50)},
        "move": {"name": args["move_name"], "power": args["move_power"],
                 "type": args["move_type"], "category": args["move_category"]},
    }
    if args.get("attacker_nature"):
        payload["attacker"]["nature"] = args["attacker_nature"]

    if is_doubles:
        payload["format"] = "doubles"
        payload["defenders"] = [
            {"species": args["defender_species"], "level": args.get("defender_level", 50)},
            {"species": args.get("defender2_species", args["defender_species"]),
             "level": args.get("defender_level", 50)},
        ]
        if args.get("ally_species"):
            payload["ally"] = {"species": args["ally_species"], "level": args.get("defender_level", 50)}
        payload["target"] = args.get("target", "both-foes")
    else:
        payload["defender"] = {"species": args["defender_species"], "level": args.get("defender_level", 50)}

    try:
        r = requests.post(DAMAGE_API, json=payload, timeout=30)
        r.raise_for_status()
        d = r.json()
        if is_doubles and d.get("targets"):
            out = {"format": "doubles", "targets": []}
            for t in d["targets"]:
                res = t.get("result", {})
                out["targets"].append({"slot": t.get("slot"), "defender": res.get("defender"),
                                        "min": res.get("minRoll"), "max": res.get("maxRoll"),
                                        "ko_summary": res.get("koSummary"),
                                        "effectiveness": res.get("effectiveness"),
                                        "target_hp": res.get("targetHp"),
                                        "spread": t.get("spreadApplied")})
            return json.dumps(out)
        return json.dumps({"echo": d.get("echo"), "rolls": d.get("rolls"),
                           "min": d.get("minRoll"), "max": d.get("maxRoll"),
                           "target_hp": d.get("targetHp"),
                           "ko_summary": d.get("koSummary"), "effectiveness": d.get("effectiveness"),
                           "stab": d.get("stab"), "modifiers": d.get("modifiers")})
    except Exception as e:
        return json.dumps({"error": str(e)})



# Caleb's team move data (power, type, category). Status moves have power 0.
TEAM_MOVES = {
    "Lucario": [
        {"name": "Aura Sphere", "power": 80, "type": "Fighting", "category": "special"},
        {"name": "Flash Cannon", "power": 80, "type": "Steel", "category": "special"},
        {"name": "Dark Pulse", "power": 80, "type": "Dark", "category": "special"},
    ],
    "Garchomp": [
        {"name": "Earthquake", "power": 100, "type": "Ground", "category": "physical"},
        {"name": "Dragon Claw", "power": 80, "type": "Dragon", "category": "physical"},
        {"name": "Stone Edge", "power": 100, "type": "Rock", "category": "physical"},
    ],
    "Mimikyu": [
        {"name": "Play Rough", "power": 90, "type": "Fairy", "category": "physical"},
        {"name": "Shadow Sneak", "power": 40, "type": "Ghost", "category": "physical"},
        {"name": "Shadow Claw", "power": 70, "type": "Ghost", "category": "physical"},
    ],
    "Corviknight": [
        {"name": "Brave Bird", "power": 120, "type": "Flying", "category": "physical"},
        {"name": "U-turn", "power": 70, "type": "Bug", "category": "physical"},
    ],
    "Greninja": [
        {"name": "Hydro Pump", "power": 110, "type": "Water", "category": "special"},
        {"name": "Ice Beam", "power": 90, "type": "Ice", "category": "special"},
        {"name": "Dark Pulse", "power": 80, "type": "Dark", "category": "special"},
        # Grass Knot power varies by target weight (20-120); 80 is the mid-range approximation
        {"name": "Grass Knot", "power": 80, "type": "Grass", "category": "special"},
    ],
    "Kingambit": [
        {"name": "Kowtow Cleave", "power": 85, "type": "Dark", "category": "physical"},
        {"name": "Sucker Punch", "power": 70, "type": "Dark", "category": "physical"},
        {"name": "Iron Head", "power": 80, "type": "Steel", "category": "physical"},
    ],
}

TEAM_NATURES = {
    "Lucario": "Timid", "Garchomp": "Jolly", "Mimikyu": "Jolly",
    "Corviknight": "Impish", "Greninja": "Timid", "Kingambit": "Adamant",
}

TEAM_HP = {
    "Lucario": 145, "Garchomp": 183, "Mimikyu": 130,
    "Corviknight": 205, "Greninja": 147, "Kingambit": 207,
}

# team.json (sitting next to this script) overrides the team above when present,
# so updating your team never means editing code. Both builds read this file.
_here = os.path.dirname(os.path.abspath(__file__))
TEAM_DETAILS = {}  # species -> {evs, item, ability, level}
TEAM_NAME = "Default Team"


def load_team():
    """(Re)load team.json into the module globals. Called at startup and after
    every team save, so the chat picks up the new team immediately."""
    global TEAM_MOVES, TEAM_NATURES, TEAM_HP, TEAM_DETAILS, TEAM_NAME
    try:
        with open(os.path.join(_here, "team.json")) as _f:
            _team = json.load(_f)
        TEAM_MOVES = _team.get("moves", TEAM_MOVES)
        TEAM_NATURES = _team.get("natures", TEAM_NATURES)
        TEAM_HP = _team.get("hp", TEAM_HP)
        TEAM_DETAILS = _team.get("details", {})
        TEAM_NAME = _team.get("name", "Custom Team")
    except Exception:
        pass


load_team()

_move_cache = {}

def get_move_data(move_name: str):
    """Look up move power/type/category from PokeAPI. Cached."""
    key = move_name.lower().replace(" ", "-")
    if key in _move_cache:
        return _move_cache[key]
    try:
        r = requests.get(f"https://pokeapi.co/api/v2/move/{key}", timeout=10)
        if r.status_code != 200:
            return None
        d = r.json()
        info = {"name": move_name, "power": d.get("power") or 0,
                "type": d["type"]["name"].capitalize(),
                "category": d["damage_class"]["name"]}
        _move_cache[key] = info
        return info
    except Exception:
        return None


# Last API error, surfaced in the UI message for debugging (temporary).
_LAST_API_ERROR = None


def _api_post_json(url, payload, timeout):
    """POST JSON. Retries transient failures (429/5xx/network); a 400 is a
    bad payload, so it's returned as an error immediately instead of retried.
    Successful calls are paced 1s apart so bursts don't trip flood protection."""
    global _LAST_API_ERROR
    for _ in range(3):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            if r.status_code == 429:
                _LAST_API_ERROR = "HTTP 429 (rate limited)"
                time.sleep(10)
                continue
            if r.status_code == 400:
                body = r.text[:200]
                _LAST_API_ERROR = f"HTTP 400: {body}"
                print(f"[AC] damage API 400. payload={json.dumps(payload)} resp={body}", flush=True)
                return {"error": body}
            r.raise_for_status()
            data = r.json()
            time.sleep(1)
            return data
        except Exception as e:
            _LAST_API_ERROR = f"{type(e).__name__}: {str(e)[:120]}"
            print(f"[AC] damage API attempt failed: {_LAST_API_ERROR}", flush=True)
            time.sleep(2)
    return None


def _api_get_json(url, params, timeout):
    """GET JSON with retries. A 429 gets a longer pause (rate limit backoff)."""
    global _LAST_API_ERROR
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                _LAST_API_ERROR = "HTTP 429 (rate limited)"
                time.sleep(10)
                continue
            if r.status_code == 200:
                data = r.json()
                time.sleep(1)
                return data
            _LAST_API_ERROR = f"HTTP {r.status_code}"
        except Exception as e:
            _LAST_API_ERROR = f"{type(e).__name__}: {str(e)[:120]}"
            print(f"[AC] moveset API attempt failed: {_LAST_API_ERROR}", flush=True)
        time.sleep(2)
    return None


# In-memory API cache: damage numbers and movesets don't change mid-session,
# so repeated questions reuse results instead of hammering the APIs
# (which is also what got the IP temporarily rate-limited).
_API_CACHE = {}
CACHE_TTL = 600  # seconds


def _cache_get(key):
    hit = _API_CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


def _cache_put(key, val):
    if val is not None:
        _API_CACHE[key] = (time.time(), val)


def _calc(attacker, defender, move, attacker_nature=None):
    """Damage calc call, trying species variants most-specific first
    (e.g. 'Mega Lucario Z' -> 'Mega Lucario' -> 'Lucario').
    Returns dict or None. Records which species the API actually used,
    so the report never claims Mega numbers from base-form math."""
    for a in _species_variants(attacker):
        for d in _species_variants(defender):
            key = ("dmg", a, d, move["name"], attacker_nature)
            hit = _cache_get(key)
            if hit is not None:
                return hit
            payload = {
                "attacker": {"species": a, "level": 50},
                "move": {"name": move["name"], "power": move["power"],
                         "type": move["type"], "category": move["category"]},
                "defender": {"species": d, "level": 50},
            }
            if attacker_nature:
                payload["attacker"]["nature"] = attacker_nature
            dj = _api_post_json(DAMAGE_API, payload, timeout=30)
            if not dj or dj.get("error"):
                continue
            res = {"move": move["name"], "power": move["power"], "type": move["type"],
                   "category": move["category"], "min": dj.get("minRoll"), "max": dj.get("maxRoll"),
                   "target_hp": dj.get("targetHp"), "ko_summary": dj.get("koSummary"),
                   "effectiveness": dj.get("effectiveness"), "stab": dj.get("stab"),
                   "attacker_used": a, "defender_used": d}
            _cache_put(key, res)
            return res
    return None


def _canon(name: str) -> str:
    """Normalize a species name for team lookups (case-insensitive):
    'Mega Lucario Z' -> 'Lucario', 'mega raichu' -> 'raichu',
    'Hisuian Arcanine' -> 'Arcanine'."""
    n = re.sub(r'(?i)mega\s+', '', name)
    n = re.sub(r'(?i)\s+[yz](?=\s|$)', '', n)
    low = n.lower()
    for prefix in ("hisuian ", "hisui ", "alolan ", "alola ",
                   "galarian ", "galar ", "paldean ", "paldea "):
        if low.startswith(prefix):
            n = n[len(prefix):]
            break
    return n.strip()


def _team_key(name):
    """Case-insensitive team lookup: 'mega raichu' -> 'Raichu'. None if unknown."""
    c = _canon(name).lower()
    for k in TEAM_MOVES:
        if k.lower() == c:
            return k
    return None


def _find_team_mon(text):
    """Which of Caleb's Pokémon is mentioned in text (case-insensitive).
    Longest match wins."""
    best, tl = None, text.lower()
    for k in TEAM_MOVES:
        if k.lower() in tl and (best is None or len(k) > len(best)):
            best = k
    return best


def _clean_foe(raw):
    """Clean a foe species from free text: strip articles/quotes, cut at the
    first comma/question/exclamation, max 3 words, title-cased."""
    raw = raw.strip().strip('"').strip()
    raw = re.sub(r'^(a|an|the)\s+', '', raw, flags=re.IGNORECASE)
    head = re.split(r'[,?!]', raw, maxsplit=1)[0]
    return " ".join(head.split()[:3]).title()


_REGION_SHORT = {"hisuian": "Hisui", "alolan": "Alola",
                 "galarian": "Galar", "paldean": "Paldea"}


def _species_variants(name: str) -> list:
    """Candidate species names for API calls, most-specific first.
    'Mega Lucario Z' -> ['Mega Lucario Z', 'Mega Lucario', 'Lucario'].
    'Hisuian Arcanine' -> ['Hisuian Arcanine', 'Arcanine-Hisui', 'Arcanine'].
    The APIs know real Megas natively with true Mega stats; Champions-original
    forms and unmodeled regional forms fall back gracefully (the report notes
    when fallback stats were used)."""
    cands = [name]
    if name.startswith("Mega "):
        no_form = name.replace(" Z", "").replace(" Y", "").strip()
        if no_form != name and no_form not in cands:
            cands.append(no_form)
    else:
        m = re.match(r'^(Hisuian|Alolan|Galarian|Paldean)\s+(.+)$', name, re.IGNORECASE)
        if m:
            hyph = f"{m.group(2).strip()}-{_REGION_SHORT[m.group(1).lower()]}"
            if hyph not in cands:
                cands.append(hyph)
    base = _canon(name)
    if base and base not in cands:
        cands.append(base)
    return [v for v in cands if v]


def _moves_from_moveset(md):
    """Extract a move-name list from either moveset API shape:
    OU 'sets[0].moves', VGC 'recommendedSet.moves', or VGC top-level
    'moves' (which are {"name":..., "pct":...} dicts)."""
    if not md:
        return []
    if md.get("sets"):
        moves = md["sets"][0].get("moves", [])
    elif isinstance(md.get("recommendedSet"), dict) and md["recommendedSet"].get("moves"):
        moves = md["recommendedSet"]["moves"]
    elif md.get("moves"):
        moves = md["moves"]
    else:
        return []
    out = []
    for m in moves:
        if isinstance(m, dict) and m.get("name"):
            out.append(m["name"])
        elif isinstance(m, str):
            out.append(m)
    return out


def analyze_matchup(my_species: str, foe_species: str) -> str:
    """Check all of Caleb's damaging moves vs foe, and foe's moveset vs Caleb.
    Returns the best move each way with full damage data.
    Worst-case philosophy: assume YOUR lowest rolls on offense,
    and the FOE's highest rolls on defense. Plan for the worst both ways."""
    my_key = _team_key(my_species)
    result = {"my_pokemon": my_species, "foe": foe_species, "my_hp": TEAM_HP.get(my_key)}

    # OFFENSE: best of my moves, compared by MIN roll (your worst rolls).
    # Pass the FULL species name so Megas get true Mega stats from the API
    # (e.g. 'Mega Lucario Z' -> API tries Mega Lucario's real 165 Attack).
    my_moves = TEAM_MOVES.get(my_key, []) if my_key else []
    if not my_moves:
        return json.dumps({"error": f"Unknown team member '{my_species}'. "
                                    f"Caleb's team: {sorted(TEAM_MOVES)}"})
    my_nature = TEAM_NATURES.get(my_key)
    best_offense = None
    for m in my_moves:
        r = _calc(my_species, foe_species, m, my_nature)
        if r and (best_offense is None or (r["min"] or 0) > (best_offense["min"] or 0)):
            best_offense = r
    result["offense"] = best_offense

    # DEFENSE: foe's moveset -> best move vs me.
    # OU/singles format first (Caleb plays singles); fall back to VGC data when
    # the OU snapshot doesn't cover the species. Tries species variants so
    # real Megas resolve natively and Champions-original forms fall back.
    foe_moves = []
    for variant in _species_variants(foe_species):
        for fmt_label, params in (("ou", {"species": variant, "format": "ou"}),
                                  ("vgc", {"species": variant})):
            key = ("moveset", variant, fmt_label)
            md = _cache_get(key)
            if md is None:
                md = _api_get_json(MOVESET_API, params, timeout=15)
                _cache_put(key, md)
            foe_moves = _moves_from_moveset(md)
            if foe_moves:
                if variant != foe_species or fmt_label != "ou":
                    result["foe_moveset_source"] = (
                        variant + ("" if fmt_label == "ou" else " (VGC data)"))
                break
        if foe_moves:
            break

    result["foe_moveset"] = foe_moves
    # DEFENSE: foe's most dangerous move, compared by MAX roll (their best rolls)
    best_defense = None
    for move_name in foe_moves:
        mi = get_move_data(move_name)
        if not mi or not mi["power"]:
            continue
        r = _calc(foe_species, my_species, mi)
        if r and (best_defense is None or (r["max"] or 0) > (best_defense["max"] or 0)):
            best_defense = r
    result["defense"] = best_defense

    # Worst-case turns, rounded up:
    #   offense -> HP / YOUR min roll (your worst rolls)
    #   defense -> HP / FOE's max roll (their best rolls)
    for side in ("offense", "defense"):
        s = result.get(side)
        if not s or not s.get("target_hp"):
            continue
        import math
        roll = s.get("min") if side == "offense" else s.get("max")
        if roll:
            s["worst_case_turns"] = math.ceil(s["target_hp"] / roll)

    return json.dumps(result)


ANALYZE_TOOL = {
    "type": "function",
    "function": {
        "name": "analyze_matchup",
        "description": (
            "Analyze a full matchup: checks all of Caleb's damaging moves against the foe "
            "and picks the best, then looks up the foe's real moveset and picks their best move "
            "against Caleb. Returns both with damage ranges, HP, KO summaries, and worst-case turns. "
            "Use this instead of calling damage_calc and get_moveset separately."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "my_species": {"type": "string", "description": "Caleb's Pokemon (e.g. 'Lucario', 'Garchomp')"},
                "foe_species": {"type": "string", "description": "Opponent's Pokemon (e.g. 'Salamence', 'Metagross')"},
            },
            "required": ["my_species", "foe_species"],
        },
    },
}

app = Flask(__name__)
client = OpenAI(base_url=LMSTUDIO_URL, api_key="lm-studio")
MODEL = None
conversations = {}  # session_id -> messages


def get_model():
    # No caching: always use whatever model is currently loaded in LM Studio.
    # (Caching caused silent reuse of the old model when Caleb switched models.)
    models = [m.id for m in client.models.list().data]
    # Llama 3.2 3B is the pick after Granite failed the grounding test.
    # Prefer an exact 3.2 match; fall back to any loaded Llama.
    llama32 = [m for m in models if "llama" in m.lower() and "3.2" in m]
    llama = [m for m in models if "llama" in m.lower()]
    pick = llama32 or llama
    if not pick:
        raise RuntimeError(f"No Llama model found. Loaded models: {models}")
    return pick[0]


@app.route("/shutdown", methods=["POST"])
def shutdown():
    """Stop the server from the UI button. Fire-and-forget: the process exits
    immediately so a fresh copy of the script can be started."""
    os._exit(0)


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/team")
def team_page():
    return render_template_string(TEAM_PAGE)


@app.route("/api/team")
def api_team():
    mons = []
    for sp in TEAM_MOVES:
        det = TEAM_DETAILS.get(sp, {})
        mons.append({"species": sp, "nature": TEAM_NATURES.get(sp, "Hardy"),
                     "level": det.get("level", 50), "evs": det.get("evs", ""),
                     "item": det.get("item", ""), "ability": det.get("ability", ""),
                     "hp": TEAM_HP.get(sp, ""),
                     "moves": [m["name"] for m in TEAM_MOVES[sp]]})
    return jsonify({"name": TEAM_NAME, "mons": mons})


@app.route("/save_team", methods=["POST"])
def save_team():
    data = request.get_json(force=True) or {}
    mons = [m for m in data.get("mons", []) if (m.get("species") or "").strip()]
    if not mons:
        return jsonify({"ok": False, "error": "Enter at least one Pokémon."})
    moves, natures, hps, details, errors = {}, {}, {}, {}, []
    seen = set()
    for m in mons:
        sp = m["species"].strip()
        if sp.lower() in seen:
            errors.append(f"Duplicate entry: {sp}")
            continue
        seen.add(sp.lower())
        mv = []
        for mn in (m.get("moves") or [])[:4]:
            mn = (mn or "").strip()
            if not mn:
                continue
            info = get_move_data(mn)
            if not info:
                errors.append(f"{sp}: move '{mn}' not found")
                continue
            mv.append({"name": info["name"], "power": info["power"],
                       "type": info["type"], "category": info["category"]})
        if not mv:
            errors.append(f"{sp}: needs at least one valid move")
            continue
        try:
            lvl = int(m.get("level") or 50)
        except (ValueError, TypeError):
            lvl = 50
        hp = None
        if str(m.get("hp") or "").strip():
            try:
                hp = int(m["hp"])
            except (ValueError, TypeError):
                hp = None
        if hp is None:
            hp = _compute_hp(sp, m.get("evs", ""))
        if hp is None:
            errors.append(f"{sp}: couldn't figure out HP — type it in manually")
            continue
        moves[sp] = mv
        natures[sp] = m.get("nature") or "Hardy"
        hps[sp] = hp
        details[sp] = {"evs": (m.get("evs") or "").strip(),
                       "item": (m.get("item") or "").strip(),
                       "ability": (m.get("ability") or "").strip(), "level": lvl}
    if errors:
        return jsonify({"ok": False, "error": "; ".join(errors)})
    team = {"name": (data.get("name") or "Custom Team").strip() or "Custom Team",
            "moves": moves, "natures": natures, "hp": hps, "details": details}
    try:
        with open(os.path.join(_here, "team.json"), "w") as f:
            json.dump(team, f, indent=2)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Couldn't write team.json: {e}"})
    load_team()
    conversations.clear()
    return jsonify({"ok": True, "count": len(moves)})


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    sid = request.remote_addr
    msgs = conversations.get(sid, [{"role": "system", "content": build_system_prompt()}])
    user_msg = data["message"]
    msgs.append({"role": "user", "content": user_msg})

    # Deterministic fast path: "X vs Y" matchup questions never touch the model.
    # Python renders the report directly from analyze_matchup data.
    parsed = _parse_matchup(user_msg)
    if parsed:
        reply = deterministic_report(parsed[0], parsed[1])
        msgs.append({"role": "assistant", "content": reply})
        conversations[sid] = msgs
        return jsonify({"reply": reply, "tools": [], "model": "deterministic"})

    tool_notes = []

    for i in range(8):
        if i < 4:
            resp = client.chat.completions.create(model=get_model(), messages=msgs,
                                                  tools=[ANALYZE_TOOL, DAMAGE_TOOL, MOVESET_TOOL],
                                                  tool_choice="auto", temperature=0)
        else:
            # The model has had 4 rounds of tool use — that's plenty.
            # Take the tools away and force the final report.
            if i == 4:
                msgs.append({"role": "user",
                             "content": "You have all the tool data you need. Write your final battle report now "
                                        "in the OFFENSE / DEFENSE / DECISION format. Do not call any more tools."})
            resp = client.chat.completions.create(model=get_model(), messages=msgs, temperature=0)
        msg = resp.choices[0].message
        msgs.append(msg)
        if not msg.tool_calls:
            conversations[sid] = msgs
            return jsonify({"reply": msg.content, "tools": tool_notes, "model": get_model()})
        for tc in msg.tool_calls:
            if tc.function.name == "analyze_matchup":
                args = json.loads(tc.function.arguments)
                result = analyze_matchup(args["my_species"], args["foe_species"])
                info = json.loads(result)
                off = info.get("offense") or {}
                dfn = info.get("defense") or {}
                tool_notes.append("Matchup analyzed: " + str(off.get("move", "?")) + " (" + str(off.get("ko_summary", "?")) + ") vs foe " + str(dfn.get("move", "?")) + " (" + str(dfn.get("ko_summary", "?")) + ")")
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            elif tc.function.name == "get_moveset":
                args = json.loads(tc.function.arguments)
                result = call_moveset_api(args)
                info = json.loads(result)
                if info.get("error"):
                    tool_notes.append("Moveset lookup: " + info["error"][:60])
                else:
                    mv = info["sets"][0]["moves"] if info.get("sets") else []
                    tool_notes.append("Looked up " + args["species"] + " moveset: " + ", ".join(mv))
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            elif tc.function.name == "damage_calc":
                args = json.loads(tc.function.arguments)
                result = call_damage_api(args)
                info = json.loads(result)
                tool_notes.append(f"Checked the calculator: {args['move_name']} — {info.get('ko_summary', '?')}")
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    conversations[sid] = msgs
    return jsonify({"reply": "(too many steps — try rephrasing)", "tools": tool_notes, "model": get_model()})


def _parse_matchup(msg):
    """Parse matchup questions -> (team_key, foe_species), or None.
    Handles 'Lucario vs Salamence, what do I do?' and
    'I sent out a Mega raichu against a Salamence'."""
    m = re.match(r'^\s*"?(.*?)"?\s+vs\.?\s+(.*)$', msg, re.IGNORECASE | re.DOTALL)
    if m:
        key = _team_key(m.group(1).strip().strip('"').strip())
        foe = _clean_foe(m.group(2))
        if key and foe:
            return key, foe
    m = re.search(r'\b(vs\.?|versus|against|into)\b', msg, re.IGNORECASE)
    if m:
        key, foe = _find_team_mon(msg[:m.start()]), _clean_foe(msg[m.end():])
        if key and foe:
            return key, foe
    return None


def _fallback_note(my_species, foe_species, off, dfn):
    """Honesty note when the damage API didn't know the exact form and fell back
    to another form's stats (e.g. Hisuian Arcanine -> Arcanine, Mega Raichu Y ->
    Raichu). A real Mega form (Mega Lucario Z -> Mega Lucario) is not a fallback."""
    fb = []
    pairs = [(my_species, off.get("attacker_used")), (my_species, dfn.get("defender_used")),
             (foe_species, off.get("defender_used")), (foe_species, dfn.get("attacker_used"))]
    for req, used in pairs:
        req, used = req.strip(), (used or "").strip()
        if not used or used.lower() == req.lower():
            continue
        if used.lower() == ("mega " + _canon(req)).lower():
            continue  # real Mega form with true Mega stats
        tag = f"{req}\u2192{used}"
        if tag not in fb:
            fb.append(tag)
    if fb:
        return ("Note: damage API lacks the exact form for " + ", ".join(fb)
                + " \u2014 numbers use fallback stats.")
    return None


def deterministic_report(my_species: str, foe_species: str) -> str:
    """OFFENSE/DEFENSE/DECISION report built purely from damage-API data.

    Worst-case philosophy (Caleb's rule): offense assumes YOUR minimum rolls,
    defense assumes the FOE's maximum rolls."""
    data = json.loads(analyze_matchup(my_species.strip(), foe_species.strip()))
    if data.get("error"):
        return "Sorry — " + data["error"]
    off, dfn = data.get("offense"), data.get("defense")
    if not off and not dfn:
        detail = f" [debug: {_LAST_API_ERROR}]" if _LAST_API_ERROR else ""
        return ("Sorry — I couldn't reach the battle-data APIs at all. "
                f"Check your internet connection and try again.{detail}")
    if not off:
        return ("Sorry — I couldn't get YOUR damage numbers for that matchup "
                "(the foe's data came through). Try again in a moment.")
    if not dfn:
        return ("Sorry — I couldn't get the FOE's damage numbers for that matchup "
                "(your data came through). Try again in a moment.")
    foe = data["foe"]
    lines = [
        f"1. OFFENSE — {off['move']} (damage range {off['min']}–{off['max']}, "
        f"worst-case {off['worst_case_turns']} turns).",
        f"2. DEFENSE — {dfn['move']} (damage range {dfn['min']}–{dfn['max']}, "
        f"worst-case {dfn['worst_case_turns']} turns).",
    ]
    ot, dt = off["worst_case_turns"], dfn["worst_case_turns"]
    if ot < dt:
        dec = (f"Attack with {off['move']} — you KO in {ot} worst-case turns, "
               f"before {foe} can KO you in {dt}.")
    elif ot > dt:
        dec = (f"Don't trade — {foe} KOs you in {dt} worst-case turns but you need {ot}. "
               f"Consider switching.")
    else:
        dec = (f"Dead-even race — both KO in {ot} worst-case turns. "
               f"It comes down to who moves first.")
    lines.append(f"3. DECISION — {dec}")
    fb = _fallback_note(my_species, foe_species, off, dfn)
    if fb:
        lines.append(fb)
    return "\n".join(lines)


if __name__ == "__main__":
    print("Aura Corner web chat → http://localhost:5000")
    app.run(host="127.0.0.1", port=5000)
