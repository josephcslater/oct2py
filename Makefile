.PHONY: all clean test cover

all:  
	make clean
	python setup.py install

clean:
	rm -rf build
	rm -rf dist
	find . -name "*.pyc" -o -name "*.py,cover"| xargs rm -f

test: 
	make clean
	python setup.py build
	cd build
	nosetests --exe -v --with-doctest 
	cd ..
	rm -rf build	
	python setup.py check -r
	
cover: 
	make clean
	pip install nose-cov
	python setup.py build
	cd build
	nosetests --exe --with-cov --cov oct2py --cov-config ../.coveragerc oct2py
	cd ..
	rm -rf build

release:
	make clean
	pip install sphinx-pypi-upload
	pip install numpydoc
	python setup.py register
	python setup.py bdist_wininst --target-version=2.7 upload
	python setup.py bdist_wininst --target-version=3.2 upload
	python setup.py bdist_wininst --target-version=3.3 upload
	python setup.py bdist_wheel upload
	python setup.py sdist --formats=gztar,zip upload
	pushd docs
	make html
	popd
	python setup.py upload_sphinx
	echo "Make sure to tag the branch"
	echo "Make sure to push to hg"
