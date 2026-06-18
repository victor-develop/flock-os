from setuptools import find_packages, setup

with open("requirements.txt") as f:
	install_requires = f.read().strip().split("\n")

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
	install_requires=install_requires,
	entry_points={"bench.utils": ["flock_os = flock_os.utils.bench_helper"]},
)
