import asyncio
import verifiers.v1 as vf
from root_cause_diagnosis import RootCauseTaskset

ts = RootCauseTaskset(vf.TasksetConfig(id="root-cause-diagnosis"))
tasks = list(ts)
print(f"loaded {len(tasks)} tasks\n")

def mk(task, reply):
    tr = vf.Trace(
        task=vf.TraceTask(type="RootCauseTask", data=task.data),
        agent=vf.AgentInfo(config=vf.AgentConfig(), name="test", trainable=False),
    )
    tr.nodes.append(vf.MessageNode(message=vf.AssistantMessage(content=reply), sampled=True))
    return tr

async def main():
    for t in tasks:
        print(f"== {t.data.name}")
        replies = [
            ("gold   ", '{"root_cause": "%s", "why": "x"}' % t.data.root_cause),
            ("symptom", '{"root_cause": "%s", "why": "x"}' % t.data.symptom_answer),
            ("garbage", "I think it is probably the network."),
        ]
        for label, reply in replies:
            tr = mk(t, reply)
            a = await t.correct_root_cause(tr)
            b = await t.stopped_at_symptom(tr)
            c = await t.valid_format(tr)
            print(f"  {label}  correct={a}  symptom={b}  format={c}  weighted={a - 0.5*b + 0.1*c:+.2f}")
        print()

asyncio.run(main())
