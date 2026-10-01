## Homework 2: Engineer a Reliable Agent

In Homework 1, you launched an agent and used it for a task where an agent mattered.

Now you will make that agent more capable, persistent, observable, reliable, and able to delegate work.

The central question is:

Can your agent use tools and memory, coordinate a small agentic team, evaluate what happens, and recover when something goes wrong?

You may continue improving your Homework 1 agent or start with a new agent.

You are not expected to code an agent framework from scratch. Start with an existing agent or coding-agent harness and configure it for a meaningful multi-step task. Most of the work should be configuration: choosing and connecting tools, writing instructions or skills, setting up memory or state, defining permissions and stopping conditions, and testing the resulting behavior. Small scripts or custom tools are welcome when useful, but they are not required.

What to configure and demonstrate
Meaningful tool use

Enable and configure at least one external or built-in tool that lets the agent take an action, retrieve information, manipulate data, run code, or interact with another system. This may be an existing tool, skill, plugin, MCP server, API integration, or command provided by your chosen harness. The tool must contribute meaningfully to the task rather than merely decorate the demo.

Persistent memory or state

Configure the agent to retain and reuse useful information across steps or across separate runs. You may use the harness's built-in memory, workspace files, session state, notes, or another existing mechanism. Show what is stored, when it is retrieved, and how it changes the agent's behavior.

An observable agent loop

Demonstrate the cycle: receive a goal, decide what to do, act through a tool, observe the result, evaluate progress, and continue or stop. You do not need to implement this loop yourself; use the traces, logs, or conversation history produced by your chosen agent. Configure a stopping condition and at least one condition under which the agent asks a person for help.

A lightweight agentic team

Configure the main agent to delegate at least one meaningful part of the task to a subagent. Give the subagent a clear role, bounded context, and an expected output. Show the handoff, the subagent's result, and how the main agent checks or uses that result. One subagent is enough, and you may use the delegation or team feature already provided by your chosen agent harness; you do not need to build a multi-agent framework.

Failure detection and recovery

Intentionally test one realistic failure, such as bad tool output, missing information, a failed command, an incorrect plan, a timeout, or an unsuccessful test. Configure or instruct the agent to detect the problem and respond by retrying, revising its plan, choosing another tool, or escalating to a person.

A small evaluation

Define 3–5 representative test cases. Compare the original or baseline configuration with your improved configuration using one or more useful measures—for example success rate, number of steps or tool calls, latency, cost, quality, or human interventions.

Your baseline is the agent before your Homework 2 improvements—normally your Homework 1 configuration if you continue that project. Run the same 3–5 test cases on both configurations.

Scope
The default command-line, chat, or web interface provided by your chosen tool is enough. You do not need to build a custom frontend, public service, agent framework, MCP or A2A interface, or complex multi-agent system for this homework. A main agent plus one built-in subagent is enough. Local execution is fine as long as the submitted evidence is clear and reproducible.

You may use OpenClaw, Hermes, Claude Code, Codex, Cursor, or another agent framework or coding tool.

Deliverables
Submit your materials directly in Canvas. You may attach multiple files in one submission; a repository or project website is not required.

Submit:

One short report (PDF preferred). Keep the report concise; there is no required page count, provided all evidence is included. The report should contain:
a brief description of the task and the agent or harness you configured;
the important configuration choices, such as model, instructions or skills, tools, memory/state, permissions, and stopping or escalation conditions;
a simple architecture or loop diagram showing the main agent, subagent, tools, and memory/state;
one trace showing a task delegated to a subagent and how its result was checked or used;
one successful execution trace;
one failure-and-recovery trace;
your results for 3–5 test cases, including the baseline-versus-improved comparison;
a short reflection explaining what improved and what still fails; and
instructions for reproducing the demonstration.
A 60–90 second demo video showing the configured agent completing the task. Upload the video to Canvas, or include a viewable video link in your report if the file is too large.
Optional supporting files, such as exported configuration, instructions, skills, screenshots, or a ZIP archive. If you wrote custom code, you may attach it or include a repository link, but custom code and a repository are not required.
Remove all passwords, API keys, tokens, and other private credentials before submitting.

Assessment
2 points — meaningful tool configuration and use
2 points — persistent memory or state
2 points — clear agent loop and meaningful subagent delegation
2 points — failure detection and recovery
2 points — evaluation, evidence, and documentation
Have fun with it!

Rubric
Homework 2: Engineer a Reliable Agent
Homework 2: Engineer a Reliable Agent
Criteria	Ratings	Pts
This criterion is linked to a Learning OutcomeMeaningful tool configuration and use
The configured tool contributes meaningfully to the task, and the submission shows how the agent uses it.
2 pts
Meets criterion
1 pts
Partially meets criterion
0 pts
Not demonstrated
2 pts
This criterion is linked to a Learning OutcomePersistent memory or state
The agent retains and retrieves useful information, and the submission shows how that state changes behavior.
2 pts
Meets criterion
1 pts
Partially meets criterion
0 pts
Not demonstrated
2 pts
This criterion is linked to a Learning OutcomeClear agent loop and meaningful subagent delegation
The submission shows the agent loop, a bounded handoff to a subagent, and how the main agent checks or uses the result.
2 pts
Meets criterion
1 pts
Partially meets criterion
0 pts
Not demonstrated
2 pts
This criterion is linked to a Learning OutcomeFailure detection and recovery
The agent is tested against a realistic failure and responds by retrying, revising, choosing another tool, or escalating appropriately.
2 pts
Meets criterion
1 pts
Partially meets criterion
0 pts
Not demonstrated
2 pts
This criterion is linked to a Learning OutcomeEvaluation, evidence, and documentation
The submission compares baseline and improved configurations on 3–5 representative tests and includes clear evidence, reflection, and reproduction instructions.
2 pts
Meets criterion
1 pts
Partially meets criterion
0 pts
Not demonstrated
2 pts
Total Points: 10