import os
import asyncio
import json
from datetime import datetime
from process.ext.utils import download_it
import urllib.parse
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql import func, tuple_, text

import sanic.exceptions
from sanic import response
from sanic import Blueprint

from db.models import db, Issuer, Plan, PlanNPIRaw, NPIData, NPIAddress, AddressArchive, NPIDataTaxonomy, \
    NPIDataTaxonomyGroup, NUCCTaxonomy

blueprint = Blueprint('npi', url_prefix='/npi', version=1)


@blueprint.get('/')
async def npi_index_status(request):
    async def get_npi_count():
        """
    The get_npi_count function returns the number of NPI records in the database.

    :return: The number of records in the npidata table
    :doc-author: Trelent
    """
    async with db.acquire():
            return await db.func.count(NPIData.npi).gino.scalar()

    async def get_npi_address_count():
        async with db.acquire():
            return await db.func.count(tuple_(NPIAddress.npi, NPIAddress.checksum, NPIAddress.type)).gino.scalar()


    npi_count, npi_address_count = await asyncio.gather(get_npi_count(), get_npi_address_count())
    data = {
        'date': datetime.utcnow().isoformat(),
        'release': request.app.config.get('RELEASE'),
        'environment': request.app.config.get('ENVIRONMENT'),
        'product_count': npi_count,
        'import_log_errors': npi_address_count,
    }

    return response.json(data)

@blueprint.get('/all')
async def get_all(request):
    start = float(request.args.get("start", 0))
    limit = float(request.args.get("limit", 0))
    classification = request.args.get("classification")
    section = request.args.get("section")
    display_name = request.args.get("display_name")
    async def get_count(classification, section, display_name):
        where = []
        if classification:
            where.append('classification = :classification')
        if section:
            where.append('section = :section')
        if display_name:
            where.append('display_name = :display_name')
        q = text(f"""select count(distinct (npi))
from mrf.npi_taxonomy,
     (select ARRAY_AGG(code) as codes from mrf.nucc_taxonomy where {' and '.join(where)}) as q
where healthcare_provider_taxonomy_code = ANY (codes);""")
        async with db.acquire() as conn:
            return (await conn.all(q, classification=classification, section=section, display_name=display_name))[0][0]

    async def get_results(start, limit, classification, section, display_name):
        where = []
        if classification:
            where.append('classification = :classification')
        if section:
            where.append('section = :section')
        if display_name:
            where.append('display_name = :display_name')

        print('where:', where)
        q = text(f"""select b.*, g.* from  mrf.npi as b, (select c.*
    from 
         mrf.npi_address as c,
         (select ARRAY_AGG(int_code) as codes from mrf.nucc_taxonomy where {' and '.join(where)}) as q
    where c.taxonomy_array && q.codes
      and c.type = 'primary'
    ORDER BY c.npi
    limit :limit offset :start) as g WHERE b.npi=g.npi;""")
        res = []
        async with db.acquire() as conn:
            for r in await conn.all(q, start=start, limit=limit, classification=classification, section=section,
                                    display_name=display_name):
                obj = {}
                count = -1
                for c in NPIData.__table__.columns:
                    count += 1
                    obj[c.key] = r[count]
                temp = NPIAddress.__table__.columns
                for c in temp:
                    count += 1
                    obj[c.key] = r[count]
                res.append(obj)
        return res

    total, rows = await asyncio.gather(
        get_count(classification, section, display_name),
        get_results(start, limit, classification, section, display_name)
    )
    return response.json({'total': total, 'rows': rows}, default=str)


@blueprint.get('/near/')
async def get_near_npi(request):
    in_long, in_lat = None, None
    if request.args.get("long"):
        in_long = float(request.args.get("long"))
    if request.args.get("lat"):
        in_lat = float(request.args.get("lat"))


    codes = request.args.get("codes")
    if codes:
        codes = [x.strip() for x in codes.split(',')]

    plan_network = request.args.get("plan_network")
    if plan_network:
        plan_network = [int(x) for x in plan_network.split(',')]
    classification = request.args.get("classification")
    section = request.args.get('section')
    display_name = request.args.get('display_name')
    name_like = request.args.get('name_like')
    exclude_npi = int(request.args.get("exclude_npi", 0))
    limit = int(request.args.get('limit', 5))
    zip_codes = []
    for zip_c in request.args.get('zip_codes', '').split(','):
        if not zip_c:
            continue
        zip_codes.append(zip_c.strip())
    radius = int(request.args.get('radius', 10))

    async with db.acquire() as conn:
        if (not (in_long and in_lat)) and zip_codes and zip_codes[0]:
            q = "select intptlat, intptlon from zcta5 where zcta5ce=:zip_code limit 1;"
            for r in await conn.all(text(q), zip_code=zip_codes[0]):
                in_long = float(r['intptlon'])
                in_lat = float(r['intptlat'])

        res = []
        extended_where = ""
        ilike_name = ""
        if exclude_npi:
            extended_where += "and a.npi <> :exclude_npi"
        if plan_network:
            extended_where += "and a.plans_network_array && (:plan_network_array)"

        where = []
        if zip_codes:
            ## fix the issue with blank in_long!!
            ## add center of zip code radius by default
            radius = 1000
            extended_where += " and SUBSTRING(a.postal_code, 1, 5) = ANY (:zip_codes)"
        if classification:
            where.append('classification = :classification')
        if section:
            where.append('section = :section')
        if display_name:
            where.append('display_name = :display_name')
        if codes:
            where.append('code = ANY(:codes)')
        if name_like:
            name_like = f'%{name_like}%'
            ilike_name += " and (d.provider_last_name ilike :name_like OR d.provider_other_organization_name ilike :name_like OR d.provider_organization_name ilike :name_like)"
        q = f"""select round(cast(st_distance(Geography(ST_MakePoint(q.long, q.lat)),
                              Geography(ST_MakePoint(:in_long, :in_lat))) / 1609.34 as numeric), 2) as distance,
       q.*, d.*
from mrf.npi as d,
(select a.* from mrf.npi_address as a,
     (select ARRAY_AGG(int_code) as codes from mrf.nucc_taxonomy where {' and '.join(where)}) as g
where ST_DWithin(Geography(ST_MakePoint(long, lat)),
                 Geography(ST_MakePoint(:in_long, :in_lat)),
                 :radius * 1609.34)
  and a.taxonomy_array && g.codes
  and a.type = 'primary'
  {extended_where}
ORDER by round(cast(st_distance(Geography(ST_MakePoint(a.long, a.lat)),
                              Geography(ST_MakePoint(:in_long, :in_lat))) / 1609.34 as numeric), 2) asc LIMIT :limit) as q WHERE q.npi=d.npi{ilike_name};
"""
        for r in await conn.all(text(q), in_long=in_long, in_lat=in_lat, classification=classification, limit=limit, radius=radius,
                                exclude_npi=exclude_npi, section=section, display_name=display_name, name_like=name_like,
                                codes=codes, zip_codes=zip_codes, plan_network_array=plan_network):
            obj = {}
            count = 0
            obj['distance'] = r[count]
            temp = NPIAddress.__table__.columns

            for c in temp:
                count += 1
                obj[c.key] = r[count]
            for c in NPIData.__table__.columns:
                count += 1
                if c.key in obj:
                    continue
                obj[c.key] = r[count]
            res.append(obj)
    return response.json(res, default=str)

@blueprint.get('/id/<npi>/full_taxonomy')
async def get_full_taxonomy_list(request, npi):
    t = []
    npi = int(npi)
    # plan_data = await db.select(
    #     [Plan.marketing_name, Plan.plan_id, PlanAttributes.full_plan_id, Plan.year]).select_from(
    #     Plan.join(PlanAttributes, ((Plan.plan_id == func.substr(PlanAttributes.full_plan_id, 1, 14)) & (
    #                 Plan.year == PlanAttributes.year)))). \
    #     group_by(PlanAttributes.full_plan_id, Plan.plan_id, Plan.marketing_name, Plan.year).gino.all()
    data = []
    async with db.acquire() as conn:
        for x in await db.select([NPIDataTaxonomy.__table__.columns,NUCCTaxonomy.__table__.columns]).where(NPIDataTaxonomy.npi == npi).where(NUCCTaxonomy.code == NPIDataTaxonomy.healthcare_provider_taxonomy_code).gino.all():
            t.append(x.to_json_dict())
    return response.json(t)


@blueprint.get('/plans_by_npi/<npi>')
async def get_plans_by_npi(request, npi):

    data = []
    plan_data = []
    issuer_data = []
    npi = int(npi)

    # async def get_plans_list(plan_arr):
    #     t = {}
    #     q = Plan.query.where(Plan.plan_id == db.func.any(plan_arr)).where(Plan.year == int(2023)).gino
    #     async with db.acquire() as conn:
    #         for x in await q.all():
    #             t[x.plan_id] = x.to_json_dict()
    #     return t

    q = db.select([PlanNPIRaw, Issuer]).where(Issuer.issuer_id == PlanNPIRaw.issuer_id).where(
        PlanNPIRaw.npi == npi).order_by(PlanNPIRaw.issuer_id.desc()).gino.load((PlanNPIRaw, Issuer))

    async with db.acquire() as conn:
        async with conn.transaction():
            async for x in q.iterate():
                data.append({'npi_info': x[0].to_json_dict(), 'issuer_info': x[1].to_json_dict()})


    return response.json({'npi_data': data, 'plan_data': plan_data, 'issuer_data': issuer_data})



@blueprint.get('/id/<npi>')
async def get_npi(request, npi):
    async def update_addr_coordinates(checksum, long, lat, formatted_address, place_id):
        async with db.acquire() as conn:
            async with conn.transaction() as tx:
                await NPIAddress.update.values(long=long,
                                               lat=lat,
                                               formatted_address=formatted_address,
                                               place_id=place_id)\
                    .where(NPIAddress.checksum == checksum).gino.status()
                temp = AddressArchive.__table__.columns
                x = await NPIAddress.query.where(NPIAddress.checksum == checksum).gino.first()
                obj = {}
                t = x.to_dict()
                for c in temp:
                    obj[c.key] = t[c.key]

        # long = long,
        # lat = lat,
        # formatted_address = formatted_address,
        # place_id = place_id
        #         del obj['checksum']
                # await AddressArchive.update.values(obj) \
                #     .where(AddressArchive.checksum == checksum).gino.status()
                await insert(AddressArchive).values([obj]).on_conflict_do_update(
                    index_elements=AddressArchive.__my_index_elements__,
                    set_=obj
                ).gino.model(AddressArchive).status()

    async def get_npi_data(npi):
        async with db.acquire():
            t = await NPIData.query.where(NPIData.npi == npi).gino.first()
        return t.to_json_dict()

    async def get_address_list(npi):
        t = []
        async with db.acquire() as conn:
            g = await NPIAddress.query.where((NPIAddress.npi == npi) & (NPIAddress.type == 'primary')).gino.all()
            for x in g:
                postal_code = x.postal_code
                if postal_code and len(postal_code)>5:
                    postal_code = f"{postal_code[0:5]}-{postal_code[5:]}"
                t_addr = ', '.join(
                    [x.first_line, x.second_line, x.city_name, f"{x.state_name} {postal_code}"])

                t_addr = t_addr.replace(' , ', ' ')
                if not (x.long and x.lat):
                    d = x.to_json_dict()
                    try:
                        raw_sql = text(f"""SELECT
                               g.rating,
                               ST_X(g.geomout) As lon,
                               ST_Y(g.geomout) As lat,
                                pprint_addy(g.addy) as formatted_address
                                from mrf.npi, 
                                standardize_address('us_lex',
                                     'us_gaz', 'us_rules', :addr) as addr,
                                geocode((
                                    (addr).house_num,  --address
                                    null,              --predirabbrev
                                    (addr).name,       --streetname
                                    (addr).suftype,    --streettypeabbrev
                                    null,              --postdirabbrev
                                    (addr).unit,       --internal
                                    (addr).city,       --location
                                    (addr).state,      --stateabbrev
                                    (addr).postcode,   --zip
                                    true,               --parsed
                                    null,               -- zip4
                                    (addr).house_num    -- address_alphanumeric
                                )::norm_addy) as g
                               where npi = :npi""")
                        addr = await conn.status(raw_sql, addr=t_addr, npi=npi)

                        if addr and len(addr[-1]) and addr[-1][0] and addr[-1][0][0] < 5:
                            d['long'] = addr[-1][0][1]
                            d['lat'] = addr[-1][0][2]
                            d['formatted_address'] = addr[-1][0][3]
                            d['place_id'] = None
                    except:
                        pass

                    update_geo = False
                    if request.app.config.get('NPI_API_UPDATE_GEOCODE') and not d['lat']:
                        update_geo = True

                    if not d['lat']:
                        try:
                            res = await AddressArchive.query.where(AddressArchive.checksum == x.checksum).gino.first()
                            if res:
                                d['long'] = res.long
                                d['lat'] = res.lat
                                d['formatted_address'] = res.formatted_address
                                d['place_id'] = res.place_id
                        except:
                            pass

                    if not d['lat']:
                        try:
                            params = {
                                request.app.config.get('GEOCODE_MAPBOX_STYLE_KEY_PARAM'): request.app.config.get(
                                    'GEOCODE_MAPBOX_STYLE_KEY')}
                            encoded_params = '.json?'.join(
                                (urllib.parse.quote_plus(t_addr), urllib.parse.urlencode(params, doseq=True),))
                            if qp:=request.app.config.get('GEOCODE_MAPBOX_STYLE_ADDITIONAL_QUERY_PARAMS'):
                                encoded_params = '&'.join((encoded_params,qp,))
                            url = request.app.config.get('GEOCODE_MAPBOX_STYLE_URL')+encoded_params
                            resp = await download_it(url)
                            geo_data = json.loads(resp.content)
                            if geo_data.get('features', []):
                                d['long'] = geo_data['features'][0]['geometry']['coordinates'][0]
                                d['lat'] = geo_data['features'][0]['geometry']['coordinates'][1]
                                d['formatted_address'] = geo_data['features'][0]['place_name']
                                d['place_id'] = None
                        except:
                            pass

                    if not d['lat']:
                        try:
                            params = {request.app.config.get('GEOCODE_GOOGLE_STYLE_ADDRESS_PARAM'): t_addr,
                                request.app.config.get('GEOCODE_GOOGLE_STYLE_KEY_PARAM'): request.app.config.get(
                                    'GEOCODE_GOOGLE_STYLE_KEY')}
                            encoded_params = urllib.parse.urlencode(params, doseq=True)
                            if qp:=request.app.config.get('GEOCODE_GOOGLE_STYLE_ADDITIONAL_QUERY_PARAMS'):
                                encoded_params = '&'.join((encoded_params,qp,))
                            url = '?'.join((request.app.config.get('GEOCODE_GOOGLE_STYLE_URL'), encoded_params,))
                            resp = await download_it(url)
                            geo_data = json.loads(resp.content)
                            if geo_data.get('results', []):
                                d['long'] = geo_data['results'][0]['geometry']['location']['lng']
                                d['lat'] = geo_data['results'][0]['geometry']['location']['lat']
                                d['formatted_address'] = geo_data['results'][0]['formatted_address']
                                d['place_id'] = geo_data['results'][0]['place_id']
                        except:
                            pass
                    if update_geo and d.get('lat'):
                        request.app.add_task(update_addr_coordinates(x.checksum, d['long'], d['lat'], d['formatted_address'], d['place_id']))
                    t.append(d)
                else:
                    t.append(x.to_json_dict())

        return t

    async def get_taxonomy_list(npi):
        t = []
        async with db.acquire():
            for x in await NPIDataTaxonomy.query.where(NPIDataTaxonomy.npi == npi).gino.all():
                t.append(x.to_json_dict())
        return t

    async def get_taxonomy_group_list(npi):
        t = []
        async with db.acquire():
            for x in await NPIDataTaxonomyGroup.query.where(NPIDataTaxonomyGroup.npi == npi).gino.all():
                t.append(x.to_json_dict())
        return t

    npi = int(npi)
    data, address_list, taxonomy_list, taxonomy_group_list = await asyncio.gather(
        get_npi_data(npi),
        get_address_list(npi),
        get_taxonomy_list(npi),
        get_taxonomy_group_list(npi)
    )
    if not data:
        raise sanic.exceptions.NotFound


    data['address_list'] = address_list
    data['taxonomy_list'] = taxonomy_list
    data['taxonomy_group_list'] = taxonomy_group_list

    return response.json(data)
