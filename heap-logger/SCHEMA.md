# heaptool --json Output Schema

## Overview

When invoked with `--json`, heaptool writes a JSON array to stdout and all progress
and summary messages to stderr. Exit code is 1 if any patterns were found above the
reporting threshold, 0 if clean. Both codes are treated as success by heap-logger.

## Top-level structure

A JSON array. Each element is one unique retention pattern, sorted most-instances-first.

```
[ <Pattern>, <Pattern>, ... ]
```

### Pattern object

```json
{
  "instance_count": 173,
  "chain": [ <ChainEntry>, <ChainEntry>, ... ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `instance_count` | integer ≥ 1 | Number of distinct objects matching this pattern. |
| `chain` | array of ChainEntry | Retention path from leaked object to GC root anchor, leaf first. |

### ChainEntry object

```json
{ "class_name": "com/playmonumenta/plugins/bosses/bosses/BossAbilityGroup$1", "field_name": "this$0" }
```

| Field | Type | Description |
|-------|------|-------------|
| `class_name` | string | JVM internal slash-separated class name. See "Class name format" below. |
| `field_name` | string or null | Field on **this** object that holds the previous (child) entry. `null` for the leaf (chain[0]), for array entries, and wherever the field name was not recoverable from the heap dump. |

### Chain ordering

`chain[0]` is the leaked object (the CraftPlayer, EntityPlayer, etc.). The last entry
is the GC root anchor (always a scheduler: CraftScheduler or CraftAsyncScheduler).
`field_name` on entry `i` is the field on entry `i` that points to entry `i-1`.

## Class name format

Class names use JVM internal slash-separated notation, not Java source dot notation.

| Kind | Example |
|------|---------|
| Ordinary class | `com/playmonumenta/plugins/Plugin` |
| Inner / anonymous class | `com/playmonumenta/plugins/bosses/BossAbilityGroup$1` |
| Object array | `java/lang/Object[]` |

Object array names have `[]` appended and the JVM `[L...;` notation stripped.
Inner class `$N` suffixes are preserved in `class_name` (they are useful for
identifying which anonymous class is leaking).

## Normalization modes

The default mode (`--no-normalize-inner-classes` NOT set) applies two collapsing rules
that make output stable for fingerprinting and easier to read:

### 1. Same-class run collapsing

Consecutive objects of the same class are collapsed to a single chain entry. A chain
of 8 `HashMap$Node` objects becomes one `HashMap$Node` entry. The count is **not**
emitted — varying depths of linked-list or hashmap traversal must not change the
fingerprint.

### 2. Class-object / instance collapsing

When a `Class<X>` object (carrying a static field reference) is immediately followed
by an instance of `X` (the class-pointer edge), the two entries are merged into one:

- `class_name` becomes `X` (the plain class name, `Class<>` wrapper stripped)
- `field_name` comes from the `Class<X>` entry (the static field name)
- The bare instance entry is dropped

This means a static field retention path like:

```
DoubleJumpManager  (.FLIGHT_SOURCES_MAP)
```

is the entry for "the static field `FLIGHT_SOURCES_MAP` on class `DoubleJumpManager`
holds the previous chain entry."

### With --no-normalize-inner-classes

Neither collapsing rule is applied. Class objects appear as `Class<X>` with a
following `X` entry; consecutive same-class runs are each emitted individually.
This mode is intended for debugging the analysis itself, not for production use.

## Full example

```json
[
  {
    "instance_count": 173,
    "chain": [
      { "class_name": "org/bukkit/craftbukkit/v1_20_R3/entity/CraftPlayer",                   "field_name": null },
      { "class_name": "java/util/HashMap$Node",                                                "field_name": "key" },
      { "class_name": "java/util/HashMap[]",                                                   "field_name": null },
      { "class_name": "java/util/HashMap",                                                     "field_name": "table" },
      { "class_name": "com/playmonumenta/plugins/managers/DoubleJumpManager",                  "field_name": "FLIGHT_SOURCES_MAP" },
      { "class_name": "com/playmonumenta/plugins/Plugin",                                      "field_name": "mDoubleJumpManager" },
      { "class_name": "org/bukkit/craftbukkit/v1_20_R3/scheduler/CraftTask",                  "field_name": "plugin" },
      { "class_name": "java/lang/Object[]",                                                    "field_name": null },
      { "class_name": "java/util/PriorityQueue",                                               "field_name": "queue" },
      { "class_name": "org/bukkit/craftbukkit/v1_20_R3/scheduler/CraftScheduler",             "field_name": "pending" }
    ]
  },
  {
    "instance_count": 2,
    "chain": [
      { "class_name": "org/bukkit/craftbukkit/v1_20_R3/entity/CraftPlayer",                   "field_name": null },
      { "class_name": "org/bukkit/event/entity/EntityDamageByEntityEvent",                     "field_name": "damager" },
      { "class_name": "org/bukkit/craftbukkit/v1_20_R3/entity/CraftWitherSkeleton",           "field_name": "lastDamageEvent" },
      { "class_name": "com/playmonumenta/plugins/bosses/bosses/TrainingDummyBoss",             "field_name": "mBoss" },
      { "class_name": "com/playmonumenta/plugins/bosses/bosses/BossAbilityGroup$1",            "field_name": "this$0" },
      { "class_name": "org/bukkit/craftbukkit/v1_20_R3/scheduler/CraftTask",                  "field_name": "rTask" },
      { "class_name": "java/lang/Object[]",                                                    "field_name": null },
      { "class_name": "java/util/PriorityQueue",                                               "field_name": "queue" },
      { "class_name": "org/bukkit/craftbukkit/v1_20_R3/scheduler/CraftScheduler",             "field_name": "pending" }
    ]
  }
]
```

## Empty output

If no candidates exceed `--min-leaked`, heaptool exits 0 with no stdout. heap-logger
treats empty stdout as "no leaks found." If candidates exist but all patterns are
suppressed by `--min-pattern-leaked`, heaptool exits 1 and writes `[]` to stdout.
