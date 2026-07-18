"""M1.1 验收：占位 Tracer 的父子 span 与渲染。"""

from agent.obs.tracer import Tracer


def test_span_parent_child_and_render():
    t = Tracer()
    with t.span("agent.run", kind="agent") as parent:
        with t.span("tool.exec", kind="tool", parent=parent):
            pass

    assert len(t.spans) == 2
    child = next(s for s in t.spans if s.parent_id == parent.id)
    assert child.name == "tool.exec"
    rendered = t.render()
    assert "agent.run" in rendered
    assert "tool.exec" in rendered
