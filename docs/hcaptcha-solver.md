# hCaptcha Visual Solver

[← Back to README](../README.md)

`CTF-pay/hcaptcha_auto_solver.py` is a standalone 4000-line solver launched by `card.py` via subprocess (ML dependencies are in an isolated venv, so cannot be imported). It is universal for any hCaptcha bridge URL, not just the Stripe scenario.

---

## Three-Layer Decision```mermaid
flowchart TB
    Start([Bridge Page Load]) --> Capture[Screenshot Current Challenge]
    Capture --> VLM{VLM<br/>Available?}
    VLM -->|Yes| VLMTry[Try VLM:<br/>Candidate Box → Direct Coordinates]
    VLM -->|No| Heuristic[Heuristic Dispatcher]
    VLMTry -->|Success| Execute[Playwright Execute<br/>Human Action Synthesis]
    VLMTry -->|Fail| Heuristic
    Heuristic --> Match{Match<br/>Known Type?}
    Match -->|Yes| Solver[Run Dedicated Solver<br/>CLIP / OpenCV / Shape IoU]
    Match -->|No| Fail([Throw unknown_prompt])
    Solver --> Execute
    Execute --> Verify{Visual Feedback<br/>Landing?}
    Verify -->|No| Retry[Offset ±10/16px<br/>Max 9 Retries]
    Verify -->|Yes| Submit[Submit + Listen<br/>checkcaptcha Response]
    Retry --> Verify
    Submit --> Done([Pass / Fail])
```---

## Layer 1 — VLM Decision (Preferred)

Call any `/v1/chat/completions` endpoint compatible with OpenAI protocol, send challenge image, candidate region overlay, structured JSON instructions. Two modes:

### Candidate Box Mode

First use OpenCV to extract candidate click/drag targets, label them `G1`, `G2`, `S1`, `T1` on the overlay, VLM selects the ID.```json
// The message sent to VLM looks roughly like this
{
  "messages": [
    {"role": "system", "content": "You are an hCaptcha solver..."},
    {"role": "user", "content": [
      {"type": "text", "text": "Prompt: please click on all the water travel"},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},   // original image
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},   // overlay with ID marked
      {"type": "text", "text": "{\"candidates\": [{\"id\": \"G1\", \"bbox\": [...]}, ...]}"}
    ]}
  ]
}
```VLM Returns:```json
{"action": "click", "selected_ids": ["G1", "G3"]}
{"action": "drag",  "source_id": "S1", "target_id": "T2"}
```### Direct Coordinate Output Mode

Fallback when candidate box extraction fails, VLM directly returns normalized coordinates:```json
{"action": "click", "coords": [[0.31, 0.42], [0.73, 0.41]]}
{"action": "drag",  "from": [0.2, 0.5], "to": [0.7, 0.5]}
```### VLM Configuration

Through environment variables:```bash
export CTF_VLM_BASE_URL="https://api.openai.com/v1"
export CTF_VLM_API_KEY="sk-..."
export CTF_VLM_MODEL="gpt-4o"
```# Or command line `--vlm-base-url` / `--vlm-api-key` / `--vlm-model` override.

---

## Layer 2 —— CLIP / OpenCV Heuristic Dispatcher

Fallback path when VLM is unavailable or fails. Dedicated solver for each known challenge type:

| Challenge Prompt Keywords | Solver | Method |
|---|---|---|
| `water travel` / `vehicle...water` | `solve_water_travel()` | CLIP binary classification, 3×3 grid or object candidates |
| `drag` / `complete the pair` | `solve_pair_drag()` | Color clustering localization + skeleton matching |
| `missing piece` / `complete the image` | `solve_missing_pieces_drag()` | HSV slot detection + shape IoU |
| `float on water` | `solve_float_on_water()` | CLIP binary classification |
| `served hot` | `solve_hot_food()` | CLIP binary classification |
| `hop or jump` / `hopping` | `solve_hop_animals()` | CLIP sliding window + clustering + two-stage scoring |
| `produce heat to work` | `solve_heat_work()` | CLIP binary classification |
| `shiny thing` | `solve_shiny_thing()` | CLIP binary classification (single choice) |
| `kept outside` | `solve_kept_outside()` | CLIP binary classification |
| `dissolve or melt` | `solve_dissolve_melt()` | CLIP multi-label classification |
| `hidden under the reference object` | `solve_hidden_under_reference()` | Edge detection + connected components |
| `complete the road` + `finish line` | `solve_road_completion()` | Edge detection + connected components |

---

## Candidate Region Extraction

Two complementary strategies, automatically selected based on image characteristics:

### Grid Mode (`detect_label_grid`)

For standard 3×3 hCaptcha grids. Detects 3 evenly-distributed bands through row/column pixel variance or non-white statistics. Decision thresholds:

- Coverage ≥ 72%
- Balance ≥ 55%

Grid path is taken if both conditions are met.

### Object Mode (`_build_object_candidates_generic`)

Fallback for non-standard layouts. Uses Canny edge detection + connected components + morphological denoising to extract independent objects.

---

## Layer 3 —— Playwright Executor

### Anti-Detection

Inject `init_script` to override:```javascript
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
// + dozen other properties
```### Human Action Synthesis

- **Click**: 4-point Bézier curve approximation + random delay (200–400ms)
- **Drag**: 3-segment interpolation path with jitter at start/middle/end
- **Pause**: Normal distribution with mean 800ms ± 300ms between operations

### Visual Feedback Retry Loop

After each interaction, capture pre and post frames and calculate two metrics:```python
changed_pixels = np.sum(np.any(frame_after != frame_before, axis=-1))
mean_diff = np.mean(np.abs(frame_after.astype(int) - frame_before.astype(int)))
```Determining "landing": `changed_pixels > THRESHOLD_PIXELS` AND `mean_diff > THRESHOLD_DIFF`.

If not landed, **automatically retry with offset**:

- Click: `±10px / ±16px` eight directions + origin point = 9 attempts
- Drag: `5 starts × 5 ends = 25` jitter combinations

### Multiple raster sources

Prioritize canvas `toDataURL()` to get the original bitmap. When canvas is blank (hCaptcha sometimes renders images to SVG), fall back to `body.screenshot()`.

### Network monitoring

Intercept two endpoints in the `hcaptcha.com` domain:```python
page.on("response", lambda r: ...)
# Intercept /getcaptcha → extract prompt / ekey
# Intercept /checkcaptcha → extract pass status
```Extracted metadata written to `round_XX.json`.

---

## Variation Retry System

Solver does not return a single answer, but rather an **ordered candidate sequence**:

### Click Class

`candidate_click_sets` sorted by CLIP confidence:

1. **Strong set**: all tiles with confidence ≥ 0.5
2. **Medium set**: all tiles with confidence ≥ 0.3
3. **Individual selection**: each candidate tile submitted separately once

### Drag Class

`build_drag_target_variations()` × `build_drag_start_variations()` each generate jitter variants:

- Source jitter: `±5px / ±10px` five points
- Target jitter: same five points
- Total combinations: 5 × 5 = 25 attempts

### Image Hash Deduplication```python
key = (prompt_text, hashlib.sha1(image_bytes).hexdigest())
exhausted_variations[key].add(variation_id)
```Failed attempts on the same problem automatically skip.

### Exhausted

All variations used up, throw:```python
class drag_variations_exhausted(Exception): pass
class click_set_variations_exhausted(Exception): pass
````card.py` triggers the daemon's "rerun current round" branch when caught.

---

## Running solver individually```bash
# Headed mode (watch it work)
~/.venvs/ctfml/bin/python CTF-pay/hcaptcha_auto_solver.py \
  http://127.0.0.1:PORT/index.html --headed --timeout 300

# Disable VLM, run heuristics only
~/.venvs/ctfml/bin/python CTF-pay/hcaptcha_auto_solver.py \
  http://127.0.0.1:PORT/index.html --no-vlm

# Custom VLM
~/.venvs/ctfml/bin/python CTF-pay/hcaptcha_auto_solver.py \
  http://127.0.0.1:PORT/index.html \
  --vlm-base-url https://api.openai.com/v1 \
  --vlm-api-key sk-xxx \
  --vlm-model gpt-4o

# Let solver directly submit verify_challenge
~/.venvs/ctfml/bin/python CTF-pay/hcaptcha_auto_solver.py \
  http://127.0.0.1:PORT/index.html \
  --verify-url      "https://api.stripe.com/v1/setup_intents/.../verify_challenge" \
  --verify-client-secret "seti_xxx_secret_xxx" \
  --verify-key      "pk_live_xxx"
```## Debug Artifacts

`--out-dir` (default `/tmp/hcaptcha_auto_solver`):

| File | Meaning |
|---|---|
| `round_XX.png` | Screenshot of each round |
| `round_XX.json` | Complete decision metadata for each round (prompt, candidate boxes, VLM response, final decision, visual feedback values) |
| `checkcaptcha_pass_*.json` | Network monitoring snapshot of the passing attempt |
| `checkcaptcha_fail_*.json` | Snapshot of the failed attempt |
| `session_meta_*.json` | Overall session metadata |

Debugging a failed challenge:```bash
# Find the most recent failure
ls -lt /tmp/hcaptcha_auto_solver_live/checkcaptcha_fail_*.json | head -1

# View the decision process
cat /tmp/hcaptcha_auto_solver_live/round_05.json | jq .
```---

## `card.py` Integration Method

`card.py` calls the solver via `subprocess`, passing the bridge URL and VLM configuration:```json
"browser_challenge": {
  "external_solver": {
    "enabled": true,
    "python": "~/.venvs/ctfml/bin/python",
    "script": "hcaptcha_auto_solver.py",
    "out_dir": "/tmp/hcaptcha_auto_solver_live",
    "timeout_s": 180,
    "headed": false,
    "vlm": {
      "enabled": true,
      "model": "gpt-4o",
      "base_url": "https://api.openai.com/v1",
      "api_key": "",
      "timeout_s": 45
    }
  }
}
````card.py::solve_stripe_hcaptcha_in_browser()` automatically supplements the above segment when it detects that a non-invisible challenge is needed and no external_solver is explicitly configured.

The solver result is passed back to `card.py` through the local bridge HTTP endpoint `/result`.

---

## Extending New Challenge Types

Three steps:

1. Write a matching function:```python
def is_carry_things_prompt(prompt: str) -> bool:
    p = prompt.lower()
    return "carry" in p and "things" in p
```# 2. Write the Solution Function:

```python
def solve(n, k):
    """
    Solves the problem for given parameters n and k.
    
    Args:
        n: The size parameter
        k: The coefficient parameter
    
    Returns:
        The result of the computation
    """
    # Initialize result variable
    result = 0
    
    # Iterate through the range
    for i in range(n):
        result += i * k
    
    return result
```

## Usage Example:

```python
# Call the solution function
answer = solve(10, 2)
print(answer)  # Output the result
```

## Key Points:

- **Function signature**: Define the function with appropriate parameters
- **Input validation**: Verify that inputs meet requirements
- **Algorithm implementation**: Write the core logic to solve the problem
- **Return value**: Ensure the function returns the correct result
- **Documentation**: Include docstrings explaining the function's purpose```python
def solve_carry_things(image: np.ndarray, prompt: str, **kw) -> SolverResult:
    # ... CLIP / OpenCV / your method
    return SolverResult(
        action="click",
        candidate_click_sets=[
            [tile_idx_1, tile_idx_2],   # strong set
            [tile_idx_1],                # fallback set
        ],
        ...
    )
```# 3. Add a branch in the dispatcher of `solve_bridge()`

```python
def solve_bridge():
    """
    Solve the bridge puzzle
    """
    # dispatcher logic
    if condition_a:
        handle_case_a()
    elif condition_b:
        handle_case_b()
    # Add new branch here
    elif condition_c:
        handle_case_c()
``````python
elif is_carry_things_prompt(prompt):
    result = solve_carry_things(image, prompt, ...)
```# PR Welcome

Please see [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Known Problem Type Coverage

Currently covers approximately 12 common hCaptcha problem types (see table above). When encountering an unfamiliar prompt:

- VLM enabled: Still attempts to output coordinates / candidate box decision directly from VLM
- VLM fails: Throws `unknown_prompt` error
- Debug information saved to `out_dir` for subsequent analysis

Adding each new problem type typically requires:

- 100–500 screenshots of that problem type (accumulated from `round_XX.png` generated by past daemon runs)
- Reading prompt text to find patterns
- Writing matching function + solver
- Integration testing

---

## Performance Tuning Recommendations

| Scenario | Recommendation |
|---|---|
| **VLM slow** | Reduce `vlm.timeout_s`, degrade early to heuristics |
| **VLM inaccurate** | Switch to stronger model (`gpt-4o` → `claude-opus-4-7`), or modify system prompt |
| **CLIP slow** | Use GPU venv (`pip install torch --index-url https://download.pytorch.org/whl/cu121`) |
| **Too many retries** | Reduce `max_click_retries` / `max_drag_retries`, let daemon re-run instead of retrying within solver |
| **Problem type not covered** | Add new solver (see above) |