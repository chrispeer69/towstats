# Process types. Railway reads this when no start command is set on the service;
# Render, Fly, Dokku and Heroku read the same file, so the deployment is not
# tied to one provider. railway.json sets the same command explicitly.
#
# ONE PROCESS, ONE WORKER, ON PURPOSE.
#
# start.py runs the dashboard AND the scheduled jobs in the same process: the
# lifespan hook in towbook_agent/web/app.py runs `alembic upgrade head` and then
# starts APScheduler on a background thread. The board is the only delivery
# mechanism now -- no SMS, no email -- so a web service that renders pages
# without refreshing them is a silent failure, and running both in one process
# makes that state unreachable.
#
# There is deliberately no `--workers` flag anywhere. N uvicorn workers would be
# N schedulers. towbook_agent/core/leader.py takes a Postgres advisory lock that
# catches it, but one worker is the configuration where it cannot arise. This is
# a handful of people reading server-rendered pages; one worker is not the
# bottleneck and never will be.
web: python start.py

# OPTIONAL and NOT enabled by default: the two-service layout. Split the
# scheduler out the day the pulls get heavy enough to slow page rendering.
#
#   1. set RUN_SCHEDULER=false on the web service
#   2. add a second service from this same repo with this start command
#
# No code change. Do not run both with RUN_SCHEDULER unset on the web service:
# the advisory lock will stop the duplicate, but you will have deployed
# something that only works because a lock is catching it.
worker: python -m towbook_agent schedule
