from job_agent.sources.ats.ashby import AshbyAdapter
from job_agent.sources.ats.greenhouse import GreenhouseAdapter
from job_agent.sources.ats.lever import LeverAdapter
from job_agent.sources.ats.workable import WorkableAdapter

ADAPTERS = {a.key: a() for a in (GreenhouseAdapter, LeverAdapter, AshbyAdapter, WorkableAdapter)}
