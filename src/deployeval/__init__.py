"""DeployEval: does an AI coding agent ship a working, secure app on AWS free tier for $0?

An open, re-runnable benchmark. The agent (any coding agent, e.g. Claude Code) is given a
plain-English brief and must design, build, and deploy the app itself; DeployEval then runs
adversarial probes against the live deployment to measure the gap between "the agent said done"
and "it actually works and is secure."
"""

__version__ = "0.1.0"
