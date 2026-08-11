"""HTTP routers.

One module per area, each exporting `router`. server.py owns the app, the
middleware and the lifespan; it includes these and nothing else routes.

Handlers here may import config, paths, the store modules and analysis_port.
Nothing here imports server.py — that direction is what the split is for.
"""
