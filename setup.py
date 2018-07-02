from setuptools import setup
from os import path

with open('README.md') as f:
    long_description = f.read()

version = '1.0.3'

setup(name='rtsp'
    , version=version
    , description='ffmpeg wrapper for RTSP client'
    , long_description=long_description
    , long_description_content_type='text/markdown'
    , author = 'Michael Stewart'
    , author_email = 'statueofmike@gmail.com'
    , url='https://github.com/statueofmike/rtsp'
    , download_url="https://github.com/statueofmike/rtsp/archive/{0}.tar.gz".format(version)
    , license='MIT'
    , packages=['rtsp']
    , include_package_data=True     # includes files from e.g. MANIFEST.in
    , classifiers=[
        'Development Status :: 5 - Production/Stable',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Topic :: Multimedia :: Video',
        'Topic :: Multimedia :: Video :: Capture',
        'Topic :: System :: Networking'
      ]
    , keywords='rtsp ffmpeg image stream'
    , install_requires=['pillow']
    , python_requires='>=2.6'
    , zip_safe=False
      )


