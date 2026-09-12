"""Frozen historical schemas for reader recovery regressions; never runtime policy.

Exact upstream blobs retained so offline tests do not depend on available Git history.
"""
import hashlib
import json

SCOPED_COMMIT = "b79f6184f53bb5ff11521cac90b8d8d1d1ccd748"
SCOPED_SHA256 = "3da5d5cc0474184d897159ddacb5a0d892b9f96bda2f04fa5e457ce84e1e6b32"
SCOPED_TEXT = r'''{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Private observed scoped-agent execution companions",
  "description": "Versioned facts beside legacy plan and Build records. An acceptance alone is staged/orphaned until a matching legacy receipt is published; no history is backfilled.",
  "type": "object",
  "required": [
    "schema_version",
    "assignments",
    "acceptances",
    "starts"
  ],
  "additionalProperties": false,
  "properties": {
    "schema_version": {
      "const": "scoped-agent-evidence.v1"
    },
    "starts": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "required": [
          "root",
          "child",
          "role"
        ]
      }
    },
    "assignments": {
      "type": "object",
      "additionalProperties": {
        "$ref": "#/$defs/assignment"
      }
    },
    "acceptances": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "required": [
          "owner",
          "assignments",
          "outputs"
        ],
        "additionalProperties": false,
        "properties": {
          "owner": {
            "type": "object"
          },
          "assignments": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": true,
            "items": {
              "type": "string"
            }
          },
          "outputs": {
            "type": "object",
            "additionalProperties": {
              "type": "string"
            }
          },
          "result_contracts": {
            "type": "object",
            "additionalProperties": {
              "$ref": "#/$defs/result_binding"
            }
          },
          "reports": {
            "type": "object",
            "additionalProperties": {
              "type": "array",
              "items": {
                "type": "object"
              }
            }
          }
        }
      }
    }
  },
  "$defs": {
    "assignment": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "id",
        "owner",
        "root",
        "purpose",
        "lens",
        "role",
        "packet_path",
        "packet_digest",
        "file_digest",
        "created_at",
        "launch",
        "child",
        "start",
        "read",
        "continuations",
        "supplements",
        "stops",
        "faults",
        "accepted"
      ],
      "properties": {
        "id": {
          "type": "string",
          "pattern": "^sa_[0-9a-f]{32}$"
        },
        "owner": {
          "type": "object"
        },
        "root": {
          "type": "string",
          "minLength": 1
        },
        "purpose": {
          "enum": [
            "review",
            "worker",
            "scout"
          ]
        },
        "lens": {
          "type": [
            "string",
            "null"
          ]
        },
        "role": {
          "type": "string",
          "minLength": 1
        },
        "packet_path": {
          "type": "string"
        },
        "packet_digest": {
          "type": "string",
          "pattern": "^sha256:[0-9a-f]{64}$"
        },
        "file_digest": {
          "type": "string",
          "pattern": "^sha256:[0-9a-f]{64}$"
        },
        "created_at": {
          "type": "string"
        },
        "launch": {
          "type": [
            "object",
            "null"
          ],
          "required": [
            "call_id",
            "provider",
            "role",
            "fresh",
            "successful",
            "input_digest"
          ],
          "properties": {
            "call_id": {
              "type": "string",
              "minLength": 1
            },
            "provider": {
              "enum": [
                "codex",
                "claude"
              ]
            },
            "role": {
              "type": "string",
              "minLength": 1
            },
            "fresh": {
              "type": "boolean"
            },
            "successful": {
              "type": "boolean"
            },
            "input_digest": {
              "type": "string"
            }
          }
        },
        "child": {
          "type": [
            "string",
            "null"
          ]
        },
        "start": {
          "type": [
            "object",
            "null"
          ]
        },
        "read": {
          "type": [
            "object",
            "null"
          ]
        },
        "continuations": {
          "type": "array",
          "items": {
            "type": "object",
            "required": [
              "call_id",
              "sender",
              "recipient",
              "content",
              "input_digest",
              "dispatched",
              "delivered"
            ],
            "properties": {
              "dispatched": {
                "type": "boolean"
              },
              "delivered": {
                "type": "boolean"
              },
              "call_id": {
                "type": "string",
                "minLength": 1
              },
              "sender": {
                "type": "string",
                "minLength": 1
              },
              "recipient": {
                "type": "string",
                "minLength": 1
              },
              "content": {
                "type": "string",
                "minLength": 1
              },
              "input_digest": {
                "type": "string",
                "minLength": 1
              }
            }
          }
        },
        "supplements": {
          "type": "array",
          "items": {
            "type": "object",
            "required": [
              "path",
              "digest",
              "call_id"
            ]
          }
        },
        "stops": {
          "type": "array",
          "items": {
            "type": "object",
            "required": [
              "child",
              "output",
              "digest",
              "continuations",
              "delivered"
            ],
            "properties": {
              "child": {
                "type": "string",
                "minLength": 1
              },
              "output": {
                "type": [
                  "string",
                  "null"
                ]
              },
              "digest": {
                "type": "string"
              },
              "continuations": {
                "type": "integer",
                "minimum": 0
              },
              "delivered": {
                "type": "boolean"
              }
            }
          }
        },
        "faults": {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        "accepted": {
          "type": "boolean"
        },
        "read_failures": {
          "description": "Failed or incomplete packet-read attempts; retained without granting read credit. Older absent fields mean no recorded attempts, not success.",
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "call_id",
              "child",
              "response_digest"
            ],
            "properties": {
              "call_id": {
                "type": [
                  "string",
                  "null"
                ]
              },
              "child": {
                "type": "string"
              },
              "response_digest": {
                "type": "string"
              }
            }
          }
        },
        "failed_launches": {
          "description": "Observed definite capacity-rejected launches retained when permitting one bounded fresh retry; absent historical facts are never inferred.",
          "type": "array",
          "maxItems": 1,
          "items": {
            "type": "object",
            "required": [
              "call_id",
              "provider",
              "role",
              "fresh",
              "successful",
              "input_digest",
              "capacity_rejected",
              "response"
            ],
            "properties": {
              "capacity_rejected": {
                "const": true
              },
              "successful": {
                "const": false
              }
            }
          }
        },
        "result_contract": {
          "$ref": "#/$defs/result_binding"
        }
      }
    },
    "result_binding": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "id",
        "mode",
        "enforcement",
        "limits",
        "schema",
        "schema_digest"
      ],
      "properties": {
        "id": {
          "type": "string"
        },
        "mode": {
          "enum": [
            "structured",
            "prose"
          ]
        },
        "enforcement": {
          "enum": [
            "canonical-ingress",
            "validation-only",
            "prose-only"
          ]
        },
        "limits": {
          "type": "object",
          "additionalProperties": {
            "type": "integer",
            "minimum": 1
          }
        },
        "schema": {
          "type": [
            "object",
            "boolean",
            "null"
          ]
        },
        "schema_digest": {
          "type": [
            "string",
            "null"
          ],
          "pattern": "^sha256:[0-9a-f]{64}$"
        }
      }
    }
  }
}
'''

PROGRAM_COMMIT = "bb339354cc6114280288fd80d2fb9977c56bcc90"
PROGRAM_SHA256 = "b0b9796339e1aa0edeba496173888c0addf9836a9985c87fda3b02bd07ec43ab"
PROGRAM_TEXT = r'''{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "engine-program.v1 — an ordered set of plans that together deliver one thing across several PRs",
  "description": "A plan is per-Build: one plan, one seal, one pull request. Work that genuinely spans several PRs was, until now, a shelf of related plans held together by memory — and the failure that produces is obligation decay, where something the first PR promised to finish in the second is simply never mentioned again. A program is the smallest object that makes that failure mechanical instead of human. What it guarantees is deliberately narrow and worth stating exactly: an obligation a plan declares it is CARRYING cannot vanish from its successor. It is satisfied, re-declared as still carried, or released with a stated reason. That is all. The program does NOT judge whether splitting the work this way was wise, whether the order is right, or whether a release was justified — those are judgment, and a mechanism that pretended to check them would only launder them. It also never auto-selects a current child: a program is a sequence someone decided on, not a queue something pops from.",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "program_id", "slug", "title", "objective", "created_at", "children"],
  "properties": {
    "schema_version": {"const": "engine-program.v1"},
    "program_id": {"type": "string", "pattern": "^prg_[0-9a-f]{12}$"},
    "slug": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*--[0-9a-f]{6}$"},
    "title": {"type": "string", "minLength": 1},
    "objective": {
      "type": "string",
      "minLength": 1,
      "description": "What the whole program delivers — the thing no single child PR delivers on its own, and the reason the split exists at all."
    },
    "created_at": {"$ref": "#/$defs/timestamp"},
    "children": {
      "type": "array",
      "items": {"$ref": "#/$defs/child"},
      "description": "The children of this program. ORDER IS NOT READ FROM THIS ARRAY: it is derived from each child's `predecessor_plan_id`, which is where the decision actually lives, and which insert re-points rather than renumbering anything. Order is a record of a decision, never a schedule anything executes: nothing here starts, selects, or advances a child."
    },
    "closure": {
      "oneOf": [{"type": "null"}, {"$ref": "#/$defs/closure"}],
      "description": "How this program ended, and the ONLY place completion is recorded. Completion used to be derived from every stored child being complete, which made 'complete' mean 'nothing left recorded' — absent successors rendered as done, and a program with one of five pull requests landed read as finished. A program is not complete while its objective is unmet, and no record can derive that: it takes the operator's explicit close. Nothing here is derived."
    },
    "closure_history": {
      "type": "array",
      "items": {"$ref": "#/$defs/erased_closure"},
      "description": "Every closure that was undone, with the reason for undoing it. Reopen is reversible for all three states — a program's completion records the operator's JUDGMENT that the objective is met, and judgments may be revisited as evidence arrives, unlike a plan's completion, which records merged history and is terminal. What must never happen is a silent reversal: what was undone, when, and why stays here."
    },
    "releases": {
      "type": "array",
      "items": {"$ref": "#/$defs/release"},
      "description": "Obligations let go at PROGRAM level, each keyed to the child that carries it. The ordinary release lives inside a successor plan's own carried_obligations, and that stays the normal door. This one exists for a debt whose successors were all abandoned: seals are terminal and abandoned plans are closed, so there is no successor left to revise, and the only honest surface remaining is the program record itself. Same price as any release — a stated reason — and refused while a live child still succeeds the carrier, because a live successor must answer instead."
    },
    "objective_history": {
      "type": "array",
      "items": {"$ref": "#/$defs/revised_objective"},
      "description": "Every prior objective text, with when it was replaced and why. An objective written before the work began goes stale as evidence arrives; nothing is overwritten silently."
    },
    "lanes": {
      "$ref": "#/$defs/lane_split",
      "description": "The concurrency split the operator has DECIDED for this program's children — which plans may ride which lane at once — with the reason for the decision stored on the split itself. Advisory only: nothing here dispatches, selects, or gates a Build, and nothing outside the lanes surface reads it (a repo-wide reader check enforces that). Absent when no split stands. A recorded split is superseded by the next `program lanes set` or withdrawn by `program lanes clear`, each of which keeps what it ended in lanes_history. Membership is the operator's decision and may ignore what `program lanes propose` recommended — a split is never refused on ordering or liveness grounds, only on input validity."
    },
    "lanes_history": {
      "type": "array",
      "items": {"$ref": "#/$defs/ended_split"},
      "description": "Every lane split that stopped standing, discriminated by HOW it ended — replaced by a successor, or cleared outright — following closure_history's replaced-vs-erased precedent rather than objective_history's replacement-only shape, so a reader of set → set → clear → set can reconstruct when no split stood. Nothing is overwritten silently: the split's own reason travels into its history entry when it ends."
    }
  },
  "$defs": {
    "timestamp": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"},
    "child": {
      "type": "object",
      "additionalProperties": false,
      "required": ["plan_id", "added_at"],
      "properties": {
        "plan_id": {"type": "string", "pattern": "^pln_[0-9a-f]{12}$"},
        "position": {
          "type": "integer",
          "minimum": 1,
          "description": "LEGACY, and no longer written. It was a display label that nothing read: the rendered ordinal is computed from the predecessor edges, so a record whose numbering has been permuted or duplicated still reads in the order its edges declare. Its survival was deferred to the verb that would first have to invent a value for an INSERTED child — insert is that verb, and the answer is that it dies rather than being renumbered, because renumbering writes a record per displaced child and opens a window where the stored number and the derived order disagree. Still permitted so records written before this remain valid; nothing writes it and nothing reads it."
        },
        "added_at": {"$ref": "#/$defs/timestamp"},
        "superseded_by": {
          "type": "string",
          "pattern": "^pln_[0-9a-f]{12}$",
          "description": "The plan that replaced this one in the order. Set by supersede, which keeps the replaced child VISIBLE rather than deleting it — history is never removed from this record — and re-points the edges so the replacement inherits its place on the chain. The replaced plan is separately retired through the Project Manager's own close path, which is what stops it being bindable to a Build; this marker is the program's side of that decision, not a second authority over it."
        },
        "predecessor_plan_id": {
          "type": "string",
          "pattern": "^pln_[0-9a-f]{12}$",
          "description": "Absent for the first child. Present for every other, and it is what the carry-forward check reads: a successor is checked against ITS declared predecessor, not against whatever happens to sit above it in the array."
        }
      }
    },
    "closure": {
      "type": "object",
      "additionalProperties": false,
      "required": ["state", "at", "reason"],
      "properties": {
        "state": {
          "enum": ["retired", "abandoned", "complete"],
          "description": "retired: superseded, kept for the record. abandoned: deliberately dropped. complete: the operator judged the objective met. All three are plain records of the same shape — state, time, reason. Completion carries no attestation of the operator's words: no local field can prove someone was present, and one implying it would be false confidence. What makes completion trustworthy is that it takes an explicit verb no derivation can reach."
        },
        "at": {"$ref": "#/$defs/timestamp"},
        "reason": {"type": "string", "minLength": 1},
        "acknowledged_unknown": {
          "type": "string",
          "minLength": 1,
          "description": "Present only when the program was closed while its obligation report held UNKNOWN entries — a missing or unreadable child, or a cycle. Unknown entries are sentences about a broken record, not obligations; nothing keyed to an obligation id can clear them, so refusing over them would point at a door that cannot open and would wedge permanently exactly the wrecked programs abandon exists for. Closing over them is therefore allowed, but only as a recorded decision: this is what was acknowledged and why."
        }
      }
    },
    "erased_closure": {
      "type": "object",
      "additionalProperties": false,
      "required": ["closure", "reopened_at", "reason"],
      "properties": {
        "closure": {"$ref": "#/$defs/closure"},
        "reopened_at": {"$ref": "#/$defs/timestamp"},
        "reason": {"type": "string", "minLength": 1}
      }
    },
    "release": {
      "type": "object",
      "additionalProperties": false,
      "required": ["child_plan_id", "obligation_id", "at", "reason"],
      "properties": {
        "child_plan_id": {
          "type": "string",
          "pattern": "^pln_[0-9a-f]{12}$",
          "description": "The child that CARRIES the obligation being released. Keyed to the child rather than released program-wide, because the same obligation id can be owed on more than one branch of a forked chain, and letting a dead branch's release clear a live branch's debt would be the silent drop this object exists to prevent."
        },
        "obligation_id": {"type": "string", "minLength": 1},
        "at": {"$ref": "#/$defs/timestamp"},
        "reason": {"type": "string", "minLength": 1}
      }
    },
    "revised_objective": {
      "type": "object",
      "additionalProperties": false,
      "required": ["objective", "replaced_at", "reason"],
      "properties": {
        "objective": {"type": "string", "minLength": 1},
        "replaced_at": {"$ref": "#/$defs/timestamp"},
        "reason": {"type": "string", "minLength": 1}
      }
    },
    "lane": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "children"],
      "properties": {
        "name": {"type": "string", "minLength": 1},
        "children": {
          "type": "array",
          "minItems": 1,
          "items": {"type": "string", "pattern": "^pln_[0-9a-f]{12}$"},
          "description": "The plan_ids riding this lane, in the operator's decided order. A child appears in at most one lane and at most once — enforced in code, because JSON Schema cannot express uniqueness across sibling arrays. Membership is what the operator decided; a child listed here need not be live, and a dead or superseded member is marked rather than removed when the split renders."
        }
      }
    },
    "lane_split": {
      "type": "object",
      "additionalProperties": false,
      "required": ["decided_at", "reason", "lanes"],
      "properties": {
        "decided_at": {"$ref": "#/$defs/timestamp"},
        "reason": {"type": "string", "minLength": 1},
        "lanes": {
          "type": "array",
          "minItems": 1,
          "items": {"$ref": "#/$defs/lane"},
          "description": "The decided lanes. Lane NAMES must be UNIQUE and no child may appear in two lanes or twice in one — both enforced in code (set_lanes refuses each with its own message), because JSON Schema cannot express uniqueness over a child key. The code is the authority; this notice records the rule so a reader of the schema alone is not misled into thinking duplicates would validate."
        }
      }
    },
    "ended_split": {
      "type": "object",
      "additionalProperties": false,
      "required": ["split", "ended_at", "ended_by", "reason"],
      "properties": {
        "split": {"$ref": "#/$defs/lane_split"},
        "ended_at": {"$ref": "#/$defs/timestamp"},
        "ended_by": {
          "enum": ["replaced", "cleared"],
          "description": "replaced: a new split took its place. cleared: it was withdrawn, with no split standing afterward. The discrimination follows closure_history's replaced-vs-erased precedent, so a later reader of set → set → clear → set can tell replacement from withdrawal and reconstruct when no split stood."
        },
        "reason": {"type": "string", "minLength": 1}
      }
    }
  }
}
'''

def historical(name):
    text = globals()[name + "_TEXT"]
    assert hashlib.sha256(text.encode()).hexdigest() == globals()[name + "_SHA256"]
    return json.loads(text)


def current_assignment(store, packet):
    """Use today's real writer with its actual closed reviewer-contract envelope."""
    from pathlib import Path
    import build_coordinator_core as core
    import reviewer_contracts
    import scoped_agents
    root = Path(scoped_agents.__file__).resolve().parents[2]
    contract = reviewer_contracts.capture(root,
        {"plan_id": "pln_0123456789ab", "revision": 1, "plan_digest": "sha256:" + "a" * 64},
        "thorough", [], ["spec-conformance"], instructions="Read fixture obligations.")
    return store.register(owner={"kind": "build", "plan": "pln_0123456789ab"},
        root="fixture-session", purpose="review", lens="spec-conformance",
        role="engine-qa-review-spec-conformance", packet=packet,
        packet_digest=core.digest(packet.read_bytes()), review_contract=contract)
