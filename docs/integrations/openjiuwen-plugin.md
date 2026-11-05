# Belle Agent plugin

Install Belle with `pip install '.[openjiuwen]'` in the host environment.

`manifest.json` identifies Belle's OpenJiuwen agent plugin. `plugin_spec()`
adds the packaged skills from `belle_agent.skills`. A skill needs an existing
`SkillUseRail` on the agent.

Some Belle rails and tools need live request state. A host builds those objects
and passes them to `belle_agent.integrations.openjiuwen.agent_plugin.load.load_plugin()`. This mounts
them through one `PluginSpec` with the packaged skills. The returned load record
can be passed to `DeepAgent.unload_extension()`.

Do not also register the same rail or tool through the host's legacy path.
Slack posting tools must retain their write-confirmation rail. Their existing
request metadata checks remain active when the plugin mounts them.
