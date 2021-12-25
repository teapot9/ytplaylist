import setuptools

from ytplaylist import (__name__, __doc__)

setuptools.setup(
    name=__name__,
    description=__doc__,

    packages=['ytplaylist'],
    entry_points={
        'console_scripts': [
            'ytplaylist = ytplaylist.download:main',
            'm3u_sanitize = ytplaylist.m3u:main_sanitize',
            'm3u_compat = ytplaylist.m3u:main_compat',
            'm3u_list = ytplaylist.m3u:main_list',
            'm3u_move = ytplaylist.m3u:main_move',
            'xspf2m3u = ytplaylist.m3u:main_xspf2m3u',
            'xspf_list = ytplaylist.m3u:main_xspf_list',
            'm3u_copy = ytplaylist.m3u:main_copy',
        ],
    },
)
