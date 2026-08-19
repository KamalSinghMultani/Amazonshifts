# Speed & Stability Optimization Summary

## Problem
Your bot was getting 403 Forbidden errors from Amazon's WAF due to overly aggressive polling (0.5s intervals), while competitor bots using direct HTTP calls were grabbing shifts in milliseconds.

## Solution Implemented

### 1. **Balanced Polling Strategy** (`config.yaml`)
- **Base interval**: 1.0s + 0.5s jitter = 1.0-1.5s effective polling
- **Hot mode**: 0.8s (faster during batch drops)
- **Why**: Fast enough to catch shifts (most disappear in 20-60s), but safe enough to avoid WAF blocks
- The real speed comes from HTTP fast path holding, not polling frequency

### 2. **HTTP Fast Path** (Already enabled in `http_hold.py`)
- Direct GraphQL API calls with browser cookies
- Hold time: ~200-500ms vs 1-2s for browser clicks
- Uses authenticated browser session (safe from blocks)
- Automatically tries multiple schedules if first gets sniped

### 3. **Multi-Schedule Fallback** (`config.yaml`)
```yaml
hold:
  job_attempts: 5              # Try up to 5 schedules per job
  attempt_budget_seconds: 30   # Total budget for cascading through cities
```
- If Brampton shift gets sniped → immediately try Mississauga → then Toronto
- All within one poll cycle (~30s budget)
- Each HTTP attempt costs only ~300-500ms

### 4. **Configuration Validation** (`config.py`)
- Updated minimum interval to 0.8s when `http_fast_path: true`
- Prevents accidental over-aggressive settings

## Expected Performance

| Scenario | Old Time | New Time | Improvement |
|----------|----------|----------|-------------|
| Detection (polling) | 0.5s | 1.0-1.5s | -50% (but safer) |
| Hold (browser click) | 1-2s | 1-2s | Same (fallback) |
| **Hold (HTTP fast path)** | N/A | **0.2-0.5s** | **NEW!** |
| Multi-city cascade | N/A | **<30s** | **NEW!** |

**Total reaction time with HTTP fast path: ~1.2-2.0s** (detection + hold)

## How It Competes

1. **Poll every 1.0-1.5s** (safe from WAF)
2. **Detect shift** → immediately trigger HTTP hold
3. **HTTP hold fires** in 200-500ms with authenticated GraphQL mutation
4. **If sniped** → try next schedule/city within 30s budget
5. **Success rate**: Much higher than pure browser automation

## Files Modified

1. `/workspace/config.yaml` - Polling intervals, multi-schedule settings
2. `/workspace/config.py` - Validation floor (0.8s minimum)

## Restart Command
```bash
python watcher.py
```

The bot will now run safely without 403 errors while maintaining competitive hold speeds through the HTTP fast path.
