from setuptools import find_packages, setup


def _read_requirements(path):
	"""Parse requirements.txt, skipping comments and blank lines."""
	with open(path) as f:
		deps = []
		for line in f:
			line = line.strip()
			if line and not line.startswith("#"):
				deps.append(line)
	return deps


setup(
	name="flock_os",
	version="0.0.1",
	description="Multi-branch organization / mega-church management SaaS on Frappe.",
	author="Flock OS",
	author_email="dev@flock.os",
	url="https://github.com/victor-develop/flock-os",
	license="MIT",
	packages=find_packages(),
	include_package_data=True,
	zip_safe=False,
	install_requires=_read_requirements("requirements.txt"),
	entry_points={"bench.utils": ["flock_os = flock_os.utils.bench_helper"]},
)
