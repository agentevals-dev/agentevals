package after_resolution

# What agentevals reads from kagent spans, keyed by the upstream group a span
# refines, never by kagent ids (those get renamed). Weaver resolves kagent's
# registry together with its pinned upstream semantic conventions, so a group
# kagent only imports is checked as well. Every finding is a violation, so
# Weaver's exit code is the result.

import rego.v1

contract := {
	"gen_ai.invoke_agent.internal": {
		"required": {
			"gen_ai.operation.name",
			"gen_ai.conversation.id",
			"gen_ai.agent.name",
			"gen_ai.input.messages",
			"gen_ai.output.messages",
			"gen_ai.usage.input_tokens",
			"gen_ai.usage.output_tokens",
			"gen_ai.response.finish_reasons",
			"error.type",
		},
		# Sessions are keyed on it.
		"floors": {"gen_ai.conversation.id": "conditionally_required"},
	},
	"gen_ai.client.inference": {
		"required": {
			"gen_ai.operation.name",
			"gen_ai.usage.input_tokens",
			"gen_ai.usage.output_tokens",
			"gen_ai.usage.cache_read.input_tokens",
			"gen_ai.usage.cache_write.input_tokens",
			"gen_ai.response.finish_reasons",
			"gen_ai.input.messages",
			"gen_ai.output.messages",
			"error.type",
		},
		"floors": {},
	},
	# gen_ai.tool.call.arguments and .result are opt in upstream, so they are not required.
	"gen_ai.execute_tool.internal": {
		"required": {
			"gen_ai.operation.name",
			"gen_ai.tool.name",
			"gen_ai.tool.call.id",
			"error.type",
		},
		"floors": {},
	},
}

# Registries from before kagent imported upstream GenAI groups (v1.0.0-alpha9)
# define only invoke_agent. Any later registry must keep all three.
legacy_schemas := {"https://kagent.dev/schemas/telemetry/0.1.0"}

rank := {"opt_in": 0, "recommended": 1, "conditionally_required": 2, "required": 3}

level_name(level) := level if is_string(level)

level_name(level) := name if {
	is_object(level)
	some name, _ in level
}

groups_of(type) := [g | some g in input.refinements.spans; g.type == type]

kagent_groups(type) := [g | some g in groups_of(type); g.id != type]

# A kagent refinement describes what kagent emits more precisely than the
# upstream group it refines, so it is checked instead when present.
checked contains [type, g] if {
	some type, _ in contract
	some g in kagent_groups(type)
}

checked contains [type, g] if {
	some type, _ in contract
	count(kagent_groups(type)) == 0
	some g in groups_of(type)
}

attribute_level(group, key) := level_name(a.requirement_level) if {
	some a in group.attributes
	a.key == key
}

deny contains finding if {
	some [type, group] in checked
	some key in contract[type].required
	not attribute_level(group, key)
	finding := {
		"id": "agentevals_contract_missing_attribute",
		"context": {"type": type, "group": group.id, "attribute": key},
		"message": sprintf("%s no longer has %s, which agentevals reads.", [group.id, key]),
		"level": "violation",
	}
}

deny contains finding if {
	some [type, group] in checked
	some key, floor in contract[type].floors
	level := attribute_level(group, key)
	rank[level] < rank[floor]
	finding := {
		"id": "agentevals_contract_requirement_floor",
		"context": {"type": type, "group": group.id, "attribute": key, "level": level, "floor": floor},
		"message": sprintf("%s lowered %s to %s; agentevals needs at least %s.", [group.id, key, level, floor]),
		"level": "violation",
	}
}

deny contains finding if {
	some type, _ in contract
	count(groups_of(type)) == 0
	not input.schema_url in legacy_schemas
	finding := {
		"id": "agentevals_contract_gone",
		"context": {"type": type},
		"message": sprintf("No span group of type %s is left, so agentevals cannot rely on it.", [type]),
		"level": "violation",
	}
}
