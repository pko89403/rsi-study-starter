You improve a Text-to-SQL agent by rewriting ONE editable block of its Python source.
Goal: the agent should produce SQL that answers the question correctly on SQLite. Do not optimize for anything else.
Rules: return the ENTIRE new block content as valid Python, keeping the same names (SYSTEM_PROMPT, FEWSHOT_IDS, build_messages). Only the block changes. Keep line breaks exactly as Python needs them.
Answer in exactly this format and nothing else:
BLOCK: <block name>
REASON: <one line>
<<<CONTENT
<new block content, multiple lines>
CONTENT>>>
