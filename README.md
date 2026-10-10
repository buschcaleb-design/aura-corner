# Aura Corner — AC

Your local AI battle coach for **Pokémon Champions** (Singles 3v3).

AC pairs a small local language model's game sense with a **real damage calculator** for the math — so you get sharp play advice without ever sending your battles to the cloud. Ask it anything mid-battle ("Garchomp vs their Corviknight — what do I do?") and it answers in three parts: your best move, their best answer, and the call.

## How it works

- **Local LLM via LM Studio** does the reasoning. AC auto-detects whatever model you have loaded — no config. (Tested with qwen3:8b.)
- **Real damage math via tool calls.** AC never guesses numbers. It calls:
  - `analyze_matchup` — checks *every* move on your Pokémon, picks your best, looks up the foe's real moveset, picks *their* best, and returns both damage ranges plus worst-case turns to KO. One call does the whole picture.
  - `damage_calc` — follow-up questions about specific moves.
  - `get_moveset` — the opponent's full moveset.
- **Never bluffs.** If a tool errors or has no data, AC says so plainly and stops — it won't invent damage ranges, HP values, or turn counts.

Every answer follows the same report format:

1. **OFFENSE** — your best move, damage range, worst-case turns to KO
2. **DEFENSE** — the foe's best move, damage range, worst-case turns until you're KO'd
3. **DECISION** — attack or switch, one line why

## Features

- **Web chat UI** — friendly browser window, just open `http://localhost:5000`
- **Team builder page** (`/team`) — enter your 6, AC coaches around them
- **Smart team builder** — type a species and its real moves + abilities load as dropdowns (via PokéAPI); Stat Points are dropdowns too (0–32 per stat, 66 total, Champions rules)
- **Deterministic matchup path** — pure-math answers with no LLM in the loop when you want just the numbers

## Quickstart

1. **LM Studio** → Developer tab → Start Server (`http://localhost:1234`), with a chat model loaded
2. `pip install flask openai requests`
3. `python team_builder.py`
4. Open `http://localhost:5000` in your browser

That's it. Type something like: *"My Garchomp vs their Corviknight"*

## Your team

Open the **Build your team** page and enter your 6 (species, nature, level, Stat Points, item, ability, moves). HP is computed automatically from base stats + Stat Points. Start typing a species and its moves and abilities become pick-lists — no more guessing movenames. Champions-original forms work too; type in HP manually if the stat lookup misses one.

## Requirements

- [LM Studio](https://lmstudio.ai/) with a chat model loaded (7–8B models run fully on GPU on an RTX 4070 laptop; 3B models fly on anything)
- Python 3.10+ with `flask`, `openai`, `requests`
- Internet access for move/stat lookups (PokéAPI) — your battles themselves never leave your machine

## Roadmap

- [x] Singles 3v3 coaching with real damage math
- [x] Web UI + smart team builder
- [ ] Turn-by-turn battle memory (rolling history across turns)
- [ ] Team-code auto-import from Poké Companion (V2, API in progress)
- [ ] VGC doubles — spread-move math, positioning
- [ ] Live play — read the game screen and advise inside the move timer

## Built with

- Move/stat data: [PokéAPI](https://pokeapi.co)
- Built by Caleb Busch, with a lot of help from Muse
