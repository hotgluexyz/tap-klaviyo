import datetime
import time
import singer
from singer import metrics
import requests
import backoff

DATETIME_FMT = "%Y-%m-%dT%H:%M:%SZ"


session = requests.Session()
logger = singer.get_logger()


def dt_to_ts(dt):
    return int(time.mktime(datetime.datetime.strptime(
        dt, DATETIME_FMT).timetuple()))


def ts_to_dt(ts):
    return datetime.datetime.fromtimestamp(
        int(ts)).strftime(DATETIME_FMT)


def update_state(state, entity, dt):
    if dt is None:
        return

    # convert timestamp int to datetime
    if isinstance(dt, int):
        dt = ts_to_dt(dt)

    if entity not in state:
        state['bookmarks'][entity] = {'since': dt}

    if dt >= state['bookmarks'][entity]['since']:
        state['bookmarks'][entity] = {'since': dt}

    logger.info("Replicated %s up to %s" % (
        entity, state['bookmarks'][entity]))


def get_starting_point(stream, state, start_date):
    if stream['stream'] in state['bookmarks'] and \
            state['bookmarks'][stream['stream']] is not None:
        return dt_to_ts(state['bookmarks'][stream['stream']]['since'])
    elif start_date:
        return dt_to_ts(start_date)
    else:
        return None


def get_latest_event_time(events):
    if "updated" in events[-1]:
        parsed_datetime = datetime.datetime.strptime(events[-1]['updated'], '%Y-%m-%dT%H:%M:%S%z')
        timestamp = parsed_datetime.timestamp()
        return ts_to_dt(int(timestamp)) if len(events) else None
    return ts_to_dt(int(events[-1]['timestamp'])) if len(events) else None


@backoff.on_exception(backoff.expo, (requests.HTTPError,requests.ConnectionError), max_tries=10, factor=2, logger=logger)
def authed_get(source, url, params):
    headers = {}
    if source in ['events',"profiles"]:
        args = singer.utils.parse_args()
        headers['Authorization'] = f"Bearer {params['api_key']}" if args.config.get("refresh_token") else f"Klaviyo-API-Key {params['api_key']}"
        logger.info(f"Auth header = {headers['Authorization']}")
        headers['revision'] = "2023-02-22"
        #override the params
        new_params = {}
        if source =="events":
            new_params['sort'] = "-datetime"
            filter_key = "datetime"
        else:
            new_params['sort'] = "updated"
            filter_key = "updated"

        if isinstance(params['since'],str):
            url = params['since']
            new_params = {}
        else:
            new_params['filter'] = f"greater-than({filter_key},{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(params['since']))})"
        params = new_params

    with metrics.http_request_timer(source) as timer:
        resp = session.request(method='get',headers=headers,url=url, params=params)
        timer.tags[metrics.Tag.http_status_code] = resp.status_code

    resp.raise_for_status()
    return resp


def get_all_using_next(stream, url, api_key, since=None):
    while True:
        r = authed_get(stream, url, {'api_key': api_key,
                                     'since': since,
                                     'sort': 'asc'})
        yield r
        if stream in ["events","profiles"]:
            r = r.json()['links']
            if 'next' in r and r['next']:
                since = r['next']
            else:
                break
        else:    
            if 'next' in r.json() and r.json()['next']:
                since = r.json()['next']
            else:
                break


def get_all_pages(source, url, api_key):
    page = 0
    while True:
        r = authed_get(source, url, {'page': page, 'api_key': api_key})
        yield r
        if r.json()['end'] < r.json()['total'] - 1:
            page += 1
        else:
            break


def get_list_members(url, api_key, id):
    marker = None
    while True:
        r = authed_get('list_members', url.format(list_id=id), {'api_key': api_key,
                                                                'marker': marker})
        response = r.json()
        records = hydrate_record_with_list_id(response.get('records'), id)
        yield records
        marker = response.get('marker')
        if not marker:
            break


def hydrate_record_with_list_id(records, list_id):
    """
    Args:
        records (array [JSON]):
        list_id (str):
    Returns:
        array of records, with the list_id appended to each record
    """
    for record in records:
        record['list_id'] = list_id

    return records

def transform_events_data(data):
    return_data = []
    for row in data:
        if "profile_id" in row['attributes']:
            if row['attributes']["profile_id"] is None:
                row['attributes']["profile_id"] = ""
        return_data.append(row['attributes'])
    return return_data  
 
def transform_profiles_data(data):
    return_data = []
    for row in data:
        row['timestamp'] = row['attributes']['updated']
        return_data.append(row['attributes'])
    return return_data   

def get_incremental_pull(stream, endpoint, state, api_key, start_date):
    latest_event_time = get_starting_point(stream, state, start_date)

    with metrics.record_counter(stream['stream']) as counter:
        if stream['stream']=="events":
            url = endpoint['events']
        elif stream['stream']=="profiles":
            url = endpoint['profiles']
        else:
            endpoint = endpoint['metric']
            url = '{}{}/timeline'.format(
                endpoint,
                stream['tap_stream_id']
            )
        for response in get_all_using_next(
                stream['stream'], url, api_key,
                latest_event_time):
            if stream['stream']=="events":
                events = response.json().get('data')
                events = transform_events_data(events)
            elif stream['stream']=="profiles":
                events = response.json().get('data')
                events = transform_profiles_data(events)
            else:
                events = response.json().get('data')    

            if events:
                counter.increment(len(events))

                singer.write_records(stream['stream'], events)

                update_state(state, stream['stream'],
                             get_latest_event_time(events))
                singer.write_state(state)

    return state


def get_full_pulls(resource, endpoint, api_key, list_ids=None):
    with metrics.record_counter(resource['stream']) as counter:
        if resource['stream'] == 'list_members':
            for id in list_ids:
                for records in get_list_members(endpoint, api_key, id):
                    if records:
                        counter.increment(len(records))
                        singer.write_records(resource['stream'], records)
        else:
            for response in get_all_pages(resource['stream'], endpoint, api_key):
                records = response.json().get('data')

                if records:
                    counter.increment(len(records))
                    singer.write_records(resource['stream'], records)
