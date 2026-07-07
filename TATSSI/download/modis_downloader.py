
"""
MODIS downloading tool to obtain data from the
Land Processes Distributed Active Archive Center (LP DAAC).
https://lpdaac.usgs.gov/

Authentication via the EarthData login.
https://urs.earthdata.nasa.gov/
"""

from functools import partial
import os
import datetime
import time
import json
import pickle
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth
from concurrent import futures

import logging
logging.basicConfig(level=logging.INFO)

LOG = logging.getLogger(__name__)
BASE_URL = "https://e4ftl01.cr.usgs.gov/"
CMR_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"

class WebError (RuntimeError):
    """An exception for web issues"""
    def __init__(self, arg):
        self.args = arg

#def read_config():
#    """
#    Read downloaders config file
#    """
#    downloaders_dir = os.path.dirname(__file__)
#    fname = os.path.join(downloaders_dir, 'config.json')
#    with open(fname) as f:
#        credentials = json.load(f)

#    url = credentials['url']
#    username = credentials['username']
#    password = credentials['password']

#    return url, username, password

def read_config():
    """
    Read downloader's config file.
    Username and password are optional now (handled by .netrc).
    """
    downloaders_dir = os.path.dirname(__file__)
    fname = os.path.join(downloaders_dir, 'config.json')
    with open(fname) as f:
        credentials = json.load(f)

    url = credentials['url']
    # Use .get() so missing keys don’t raise KeyError
    username = credentials.get('username')
    password = credentials.get('password')

    return url, username, password


def save_available_dates(product, avail_dates):
    """
    Save the available dates for a specific product on a pickle file
    in $HOME/.TATSSI/{product}
    """
    homedir = os.path.expanduser("~")
    config_dir = os.path.join(homedir, '.TATSSI')
    # Create TATSSI config dir 
    Path(config_dir).mkdir(parents=True, exist_ok=True)

    fname = os.path.join(config_dir, product)

    with open(fname, 'wb') as f:
        pickle.dump(avail_dates, f)

    f.close()

def get_available_dates_from_cache(product):
    """
    Get the available dates for a specific product from the
    TATSSI config dir
    """
    homedir = os.path.expanduser("~")
    config_dir = os.path.join(homedir, '.TATSSI')

    fname = os.path.join(config_dir, product)

    if os.path.exists(fname) is True:
        with open(fname, 'rb') as f:
            avail_dates = pickle.load(f)
    else:
        avail_dates = []

    return avail_dates

#def get_available_dates(url, product, start_date, end_date=None,
#                        use_cache=False):
#    """
#    This function gets the available dates for a particular
#    product, and returns the ones that fall within a particular
#    pair of dates. If the end date is set to ``None``, it will
#    be assumed it is today. If use_cache is True then first the
#    available dates will be first obtained from any cache available.
#    """
#    if use_cache is True:
#        avail_dates = get_available_dates_from_cache(product)
#    else:
#        avail_dates = []

#    if end_date is None:
#        end_date = datetime.datetime.now()
#    r = requests.get(url)

#    if not r.ok:
#        raise WebError(
#            "Problem contacting NASA server. Either server " +
#            "is down, or the product you used (%s) is kanckered" %
#            url)
#    html = r.text

#    for line in html.splitlines()[19:]:
#        if line.find("[DIR]") >= 0 and line.find("href") >= 0:
#            this_date = line.split("href=")[1].split('"')[1].strip("/")
#            this_datetime = datetime.datetime.strptime(this_date,
#                                                       "%Y.%m.%d")
#            if this_datetime >= start_date and this_datetime <= end_date:
#                avail_dates.append(url + "/" + this_date)

    # Save search to cache
#    if len(avail_dates) > 0:
#        save_available_dates(product, avail_dates)

#    return avail_dates
  

def get_available_dates(product, start_date, end_date=None, use_cache=False):
    """
    Query NASA CMR API for MODIS product granules between start_date and end_date.
    Returns a list of download URLs.
    """
    if use_cache:
        avail_dates = get_available_dates_from_cache(product)
    else:
        avail_dates = []

    if end_date is None:
        end_date = datetime.datetime.now()

    short_name, version = product.split(".")  # e.g. MOD13Q1.061 → MOD13Q1, 061

    params = {
        "short_name": short_name,
        "version": version,
        "temporal": f"{start_date.isoformat()}Z,{end_date.isoformat()}Z",
        "page_size": 2000
    }

    r = requests.get(CMR_URL, params=params)
    if not r.ok:
        raise WebError(f"Problem contacting NASA CMR API: {r.text}")

    data = r.json()
    urls = []
    for entry in data.get("feed", {}).get("entry", []):
        for link in entry.get("links", []):
            href = link.get("href", "")
            # Keep only HTTPS data links that end with .hdf
            if "data" in link.get("rel", "") and href.startswith("https") and href.endswith(".hdf"):
            	urls.append(href)
#                urls.append(link["href"])

    if urls:
        save_available_dates(product, urls)

    return urls

def download_tile_list(url, tiles):
    """
    For a particular product and date, obtain the data tile URLs.
    """
    if not isinstance(tiles, type([])):
        tiles = [tiles]
    while True:
        try:
            r = requests.get(url )
            break
        except requests.execeptions.ConnectionError:
            time.sleep(240)
            
    grab = []
    for line in r.text.splitlines():
        for tile in tiles:
            if line.find ( tile ) >= 0 and line.find (".xml" ) < 0 \
                    and line.find("BROWSE") < 0:
                fname = line.split("href=")[1].split('"')[1]
                grab.append(url + "/" + fname)
    return grab

#def download_tiles(url, session, username, password, output_dir):
#    basicAuth = HTTPBasicAuth(username,password)

#    r1 = session.request('get', url)
#    r = session.get(r1.url,auth=basicAuth, stream=True)
#    fname = url.split("/")[-1]
#    LOG.debug("Getting %s from %s(-> %s)" % (fname, url, r1.url))

#    if not r.ok:
#        # raise IOError(f"Can't start download... {fname}")
#        print(f"Can't start download... {fname}. Try download again.")
#        return

#    file_size = int(r.headers['content-length'])
#    LOG.debug("\t%s file size: %d" % (fname, file_size))
#    output_fname = os.path.join(output_dir, fname)

#    # Save with temporary filename...
#    with open(output_fname+".partial", 'wb') as fp:
#        for block in r.iter_content(65536):
#            fp.write(block)

#    # Rename to definitive filename
#    os.rename(output_fname+".partial", output_fname)
#    LOG.info("Done with %s" % output_fname)
#    return output_fname

def download_tiles(url, session, username, password, output_dir):
    """
    Download a single MODIS tile using Earthdata authentication.
    Authentication is handled via ~/.netrc, so username/password
    are optional and not required here.
    """
    # First request (to resolve redirects if any)
    r1 = session.get(url, stream=True)
    fname = url.split("/")[-1]
    LOG.debug("Getting %s from %s" % (fname, url))

    if not r1.ok:
        print(f"Can't start download... {fname}")
        print(f"Status code: {r1.status_code}")
        print(f"Response headers: {r1.headers}")
        print(f"Response text (first 200 chars): {r1.text[:200]}")
        return None

    # File size (if provided)
    file_size = int(r1.headers.get('content-length', 0))
    LOG.debug("\t%s file size: %d" % (fname, file_size))
    output_fname = os.path.join(output_dir, fname)

    # Save with temporary filename...
    with open(output_fname + ".partial", 'wb') as fp:
        for block in r1.iter_content(65536):
            fp.write(block)

    # Rename to definitive filename
    os.rename(output_fname + ".partial", output_fname)
    LOG.info("Done with %s" % output_fname)
    return output_fname

def required_files (url_list, output_dir):
    """
    Checks for files that are already available in the system.
    """
    
    all_files_present = os.listdir (output_dir)
    hdf_files_present = [fich 
                        for fich in all_files_present if fich.endswith(".hdf")]
    hdf_files_present = set(hdf_files_present)
    
    flist= [url.split("/")[-1] for url in url_list]
    file_list = dict(zip(flist, url_list))
    flist = set(flist)
    files_to_download = list(flist.difference(hdf_files_present))
    to_download = [ file_list[k] for k in files_to_download]

    return to_download
    
#def get_modis_data(platform, product, tiles, 
#                   output_dir, start_date,
#                   end_date=None, n_threads=5,
#                   username=None, password=None,
#                   progressBar=None,
#                   use_cache=False):
#    """The main workhorse of MODIS downloading. This function will grab
#    products for a particular platform (MOLT, MOLA or MOTA). The products
#    are specified by their MODIS code (e.g. MCD45A1.051 or MOD09GA.006).
#    You need to specify a tile (or a list of tiles), as well as a starting
#    and end date. If the end date is not specified, the current date will
#    be chosen. Additionally, you can specify the number of parallel threads
#    to use. And you also need to give an output directory to dump your files.

#    Parameters
#    -----------
#    username: str
#        The username that is required to download data from the MODIS archive.
#    password: str
#        The password required to download data from the MODIS archive.
#    platform: str
#        The platform, MOLT, MOLA or MOTA. This basically relates to the sensor
#        used (or if a combination of AQUA & TERRA is used).
#        MOLT - Terra only products
#        MOLA - Aqua only products
#        MOTA - Combined TERRA & AQUA products
#    product: str
#        The MODIS product. The product name should be in MODIS format
#        (MOD09Q1.006, so product acronym dot collection)
#    tiles: str or iter
#        A string with a single tile (e.g. "h17v04") or a list of such strings
#        ["h17v03", "h17v04"].
#    output_dir: str
#        The output directory
#    start_date: datetime
#        The starting date as a datetime object
#    end_date: datetime
#        The end date as a datetime object. If not specified, taken as today.
#    n_threads: int
#        The number of concurrent downloads to envisage. I haven't got a clue
#        as to what a good number would be here...
#    use_cache: boolean
#        Whether to use local cache with results from previous searches

#    """
#    # Read config
#    BASE_URL, _username, _password = read_config()
#    if username is not None:
#        username = _username
#    if password is not None:
#        password = _password

    # Ensure the platform is OK
#    assert platform.upper() in [ "MOLA", "MOLT", "MOTA"], \
#        "%s is not a valid platform. Valid ones are MOLA, MOLT, MOTA" % \
#        platform

    # If output directory doesn't exist, create it
#    if not os.path.exists(output_dir):
#        os.mkdir(output_dir)

    # Cook the URL for the product
#    url = BASE_URL + platform + "/" + product
    # Get all the available dates in the NASA archive...
#    the_dates = get_available_dates(url, product, start_date,
#            end_date=end_date, use_cache=use_cache)
    
    # We then explore the NASA archive for the dates that we are going to
    # download. This is done in parallel. For each date, we will get the
    # url for each of the tiles that are required.
#    the_tiles = []
#    download_tile_patch = partial(download_tile_list, tiles=tiles)
#    with futures.ThreadPoolExecutor(max_workers=n_threads) as executor:
#        for tiles in executor.map(download_tile_patch, the_dates):
#            the_tiles.append(tiles)

    # Flatten the list of lists...
#    gr = [g for tile in the_tiles for g in tile]
#    gr.sort()

    # Check whether we have some files available already
#    gr_to_dload = required_files(gr, output_dir)
#    gr = gr_to_dload
  
#    msg = f"Will download {len(gr)} files..."
#    if progressBar is not None:
#        progressBar.setFormat(msg)

#    LOG.info("Will download %d files" % len ( gr ))
#    # Wait for a few seconds before downloading the data
#    time.sleep(5)

    # The main download loop. This will get all the URLs with the filenames,
    # and start downloading them in parallel.
#    dload_files = []
#    with requests.Session() as s:
        #s.auth = (username, password)
#        download_tile_patch = partial(download_tiles,
#                session=s,
#                output_dir=output_dir,
#                username=username,
#                password=password )
        
#        with futures.ThreadPoolExecutor(max_workers=n_threads) as executor:
#            for i, fich in enumerate(executor.map(download_tile_patch, gr)):
#                dload_files.append(fich)
#                if progressBar is not None:
#                    progressBar.setValue((len(dload_files) / len(gr)) * 100.0)
        
#    return dload_files

def get_modis_data(platform, product, tiles, 
                   output_dir, start_date,
                   end_date=None, n_threads=5,
                   username=None, password=None,
                   progressBar=None,
                   use_cache=False):
    """
    Main MODIS downloader using CMR API instead of legacy HTML scraping.
    """
    # Read config
    _, _username, _password = read_config()
    if username is None:
        username = _username
    if password is None:
        password = _password

    # Ensure output directory exists
    if not os.path.exists(output_dir):
        os.mkdir(output_dir)

    # Get available granule URLs
    granule_urls = get_available_dates(product, start_date,
                                       end_date=end_date,
                                       use_cache=use_cache)

    # Filter by tile IDs
    selected_urls = []
    if not isinstance(tiles, list):
        tiles = [tiles]
    for url in granule_urls:
        if any(tile in url for tile in tiles):
            selected_urls.append(url)

    # Check which files are missing locally
    urls_to_download = required_files(selected_urls, output_dir)

    LOG.info("Will download %d files" % len(urls_to_download))
    if progressBar is not None:
        progressBar.setFormat(f"Will download {len(urls_to_download)} files...")

    time.sleep(5)

    dload_files = []
    with requests.Session() as s:
        s.auth = (username, password)
        download_tile_patch = partial(download_tiles,
                                      session=s,
                                      output_dir=output_dir,
                                      username=username,
                                      password=password)
        with futures.ThreadPoolExecutor(max_workers=n_threads) as executor:
            for i, fich in enumerate(executor.map(download_tile_patch, urls_to_download)):
                dload_files.append(fich)
                if progressBar is not None:
                    progressBar.setValue((len(dload_files) / len(urls_to_download)) * 100.0)

    return dload_files
