import click

from flock_os import __version__


@click.group()
def flock_os():
	"""Flock OS bench utilities."""


@flock_os.command("version")
def version():
	"""Print the installed flock_os app version."""
	click.echo(__version__)
