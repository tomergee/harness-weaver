from k8s_agent_sandbox import SandboxClient

c = SandboxClient()
s = c.create_sandbox("python", namespace="default", sandbox_ready_timeout=300)
try:
    r = s.commands.run("python3 -c 'print(2+2)'")
    print("exit", r.exit_code, "stdout", repr(r.stdout), "stderr", repr(r.stderr))
finally:
    s.close_connection()
    s.terminate()
