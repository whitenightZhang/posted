import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sigfig import round

from posted.path import pathOfTEDFile
from posted.config.config import techs, flowTypes, defaultUnits, defaultMasks
from posted.ted.TEBase import TEBase
from posted.ted.TEDataFile import TEDataFile
from posted.ted.TEDataTable import TEDataTable
from posted.units.units import convUnitDF, convUnit, ureg


class TEDataSet(TEBase):
    # initialise
    def __init__(self,
                 tid: str,
                 load_other: None | list = None,
                 load_database: bool = False,
                 skip_checks: bool = False,
                 ):
        TEBase.__init__(self, tid)

        # initialise object fields
        self._df: None | pd.DataFrame = None

        # read TEDataFiles and combine into dataset
        self._loadFiles(load_other, load_database, skip_checks)

        # adjust units: set default reference and reported units and normalise
        self._adjustUnits()


    # load TEDatFiles and compile into dataset
    def _loadFiles(self, load_other: None | list, load_database: bool, skip_checks: bool):
        files = []

        # load default TEDataFile from POSTED database
        if not load_other or load_database:
            files.append(TEDataFile(self._tid, pathOfTEDFile(self._tid)))

        # load TEDataFiles specified as arguments
        if load_other is not None:
            for o in load_other:
                if isinstance(o, TEDataFile):
                    files.append(o)
                elif isinstance(o, Path) or isinstance(o, str):
                    p = o if isinstance(o, Path) else Path(o)
                    files.append(TEDataFile(self._tid, p))
                else:
                    raise Exception(f"Unknown load type: {type(o).__name__}")

        # raise exception if no TEDataFiles can be loaded
        if not files:
            raise Exception(f"No TEDataFiles to load for technology '{self._tid}'.")

        # load all TEDataFiles and check consistency
        for f in files:
            f.load()
            if not skip_checks:
                f.check()

        # compile dataset from the dataframes loaded from the individual files
        self._df = pd.concat([f.data for f in files])


    # adjust units: set default reference and reported units and normalise
    def _adjustUnits(self):
        # set default reference units for all entry types
        self._setRefUnitsDef()

        # normalise reference units of all entries
        self._normRefUnits()

        # set default reported units for all entry types
        self._setRepUnitsDef()

        # normalise reported units of all entries
        self._normRepUnits()


    # determine default reference units of entry types from technology class
    def _setRefUnitsDef(self):
        self._refUnits = {}
        for typeid in self._tspecs['entry_types']:
            # set to nan if entry type has no reference dimension
            if 'ref_dim' not in self._tspecs['entry_types'][typeid]:
                self._refUnits[typeid] = np.nan
            else:
                # get reference dimension
                refDim = self._tspecs['entry_types'][typeid]['ref_dim']

                # create a mapping from dimensions to default units
                unitMappings = defaultUnits.copy()
                if 'reference_flow' in self._tspecs:
                    unitMappings |= {'[flow]': flowTypes[self._tspecs['reference_flow']]['default_unit']}

                # map reference dimensions to default reference units
                self._refUnits[typeid] = refDim
                for dim, unit in unitMappings.items():
                    self._refUnits[typeid] = self._refUnits[typeid].replace(dim, unit)


        # override with default reference unit of specific technology
        if 'default-ref-units' in self._tspecs:
            self._refUnits |= self._tspecs['default-ref-units']


    # normalise reference units
    def _normRefUnits(self):
        # default reference value is 1.0
        self._df['reference_value'].fillna(1.0, inplace=True)

        # add default reference unit conversion factor
        self._df['reference_unit_default'] = self._df['type'].map(self._refUnits).astype(str)
        self._df['reference_unit_factor'] = np.where(
            self._df['reference_unit'].notna(),
            convUnitDF(self._df, 'reference_unit', 'reference_unit_default', self.referenceFlow),
            1.0,
        )

        # set converted value and unit
        self._df.insert(7, 'value',
            self._df['reported_value'] \
          / self._df['reference_value'] \
          / self._df['reference_unit_factor']
        )
        self._df.insert(8, 'unc',
            self._df['reported_unc'] \
          / self._df['reference_value'] \
          / self._df['reference_unit_factor']
        )
        self._df.insert(9, 'unit', self._df['reported_unit'])

        # drop old unit and value columns
        self._df.drop(
            self._df.filter(regex=r"^(reported|reference)_(value|unc|unit).*$").columns.to_list(),
            axis=1,
            inplace=True,
        )


    # set units of entries
    def _setRepUnitsDef(self):
        types = set(self._df['type'].unique().tolist() + ['fopex', 'fopex_spec'])
        self._repUnits = []
        for typeid in types:
            # get reported dimension of entry type
            repDim = self._tspecs['entry_types'][typeid]['rep_dim']

            # map reported dimensions to target reported units
            repUnit = repDim
            for dim, unit in defaultUnits.items():
                repUnit = repUnit.replace(dim, unit)
            if '[flow]' not in repUnit:
                self._repUnits.append({'type': typeid, 'unit': repUnit})
            else:
                for flowid in self._df.query(f"type=='{typeid}'")['flow_type'].unique():
                    repUnitFlow = repUnit.replace('[flow]', flowTypes[flowid]['default_unit'])
                    self._repUnits.append({'type': typeid, 'flow_type': flowid, 'unit': repUnitFlow})


    # normalise reported units
    def _normRepUnits(self):
        self._df = self._df.merge(
            pd.DataFrame.from_records(self._repUnits).rename(columns={'unit': 'unit_convert'}),
            on=['type', 'flow_type'],
        )
        convFactor = convUnitDF(self._df, 'unit', 'unit_convert')
        self._df['value'] *= convFactor
        self._df['unc'] *= convFactor
        self._df['unit'] = self._df['unit_convert']
        self._df.drop(columns=['unit_convert'], inplace=True)


    # convert values to defined units (use defaults if non provided)
    def convertUnits(self, type_units: None | dict = None, flow_units: None | dict = None):
        # raise exception if no updates to units are provided
        if type_units is None and flow_units is None:
            return

        # update reported units of dataset from function argument
        for record in self._repUnits:
            if type_units is not None and record['type'] in type_units:
                record['unit'] = type_units[record['type']]
            elif flow_units is not None and 'flow_type' in record and record['flow_type'] in flow_units:
                record['unit'] = flow_units[record['flow_type']]

        # normalise reported units
        self._normRepUnits()

        return self


    # access dataframe
    @property
    def data(self):
        return self._df


    # get reported unit for entry type
    def getRepUnit(self, typeid: str, flowid: str | None = None):
        if flowid is None:
            return next(e['unit'] for e in self._repUnits if e['type'] == typeid)
        else:
            return next(e['unit'] for e in self._repUnits if e['type'] == typeid and e['flow_type'] == flowid)


    # get reference unit for entry type
    def getRefUnit(self, typeid: str):
        return self._refUnits[typeid]


    # select data
    def generateTable(self,
                      agg: None | list = None,
                      masks_database: bool = True,
                      masks_other: None | list = None,
                      keepSingularIndexLevels: bool = False,
                      **kwargs):
        # the dataset it the starting-point for the table
        table = self._df.copy()

        # drop columns that are not considered
        table.drop(columns=['region', 'unc', 'comment', 'src_comment'], inplace=True)

        # apply quick fixes
        table = self._applyTypeMappings(table)

        # combine type, flow_type, and unit columns
        table['type'] = table.apply(
            lambda row: f"{row['type']}{':'+str(row['flow_type']) if row.notna()['flow_type'] else ''} [{row['unit']}]",
            axis=1,
        )
        table.drop(columns=['flow_type', 'unit'], inplace=True)

        # insert missing periods
        table = self._insertMissingPeriods(table)

        # query by selected sources
        if 'src_ref' not in kwargs or kwargs['src_ref'] is None:
            pass
        elif isinstance(kwargs['src_ref'], str):
            table = table.query(f"src_ref=='{kwargs['src_ref']}'")
        elif isinstance(kwargs['src_ref'], list):
            table = table.query(f"src_ref.isin({kwargs['src_ref']})")

        # expand all case fields
        expandCols = {}
        for idxName, colSpecs in self._tspecs['case_fields'].items():
            if (idxName not in kwargs or kwargs[idxName] is None) and colSpecs:
                expandCols[idxName] = colSpecs['options']
            elif idxName in kwargs and kwargs[idxName] is not None and isinstance(kwargs[idxName], str):
                expandCols[idxName] = [kwargs[idxName]]
            elif idxName in kwargs and kwargs[idxName] is not None and isinstance(kwargs[idxName], list):
                expandCols[idxName] = kwargs[idxName]
        table = self._expandTechs(table, expandCols)

        # group by identifying columns and select periods/generate time series
        if 'period' not in kwargs or kwargs['period'] is None:
            period = datetime.date.today().year
        else:
            period = kwargs['period']
        if isinstance(period, int) | isinstance(period, float):
            period = [period]
        table = self._selectPeriods(table, period)

        # apply masks
        table = self._applyMasks(table, masks_other, masks_database)

        # sort table
        sorting = ['type'] + self._caseFields + ['src_ref', 'period', 'component']
        table = table.sort_values(by=sorting).reset_index(drop=True)

        # aggregation
        if agg is None:
            agg = ['src_ref']
        if len(period) == 1 and 'period' not in agg:
            agg += ['period']
        groupForSum = [c for c in table.columns if c not in ['component', 'value']]
        groupForAgg = [c for c in groupForSum if c not in agg]
        table['value'].fillna(0.0, inplace=True)
        table = table \
            .groupby(groupForSum, dropna=False) \
            .agg({'value': 'sum'}) \
            .groupby(groupForAgg, dropna=False) \
            .agg({'value': 'mean'})

        # unstack type
        table = table['value'].unstack('type')

        # rename case fields
        table.index.rename([
            (f"{idxName}:{self._tid}" if idxName in self._tspecs['case_fields'] else idxName)
            for idxName in table.index.names],
            inplace=True,
        )

        # round values
        table = table.apply(lambda col: col.apply(lambda cell:
            cell if cell!=cell else round(cell, sigfigs=4, warn=False)
        ))

        # move units from column name to pint column unit
        for typeName in table.columns:
            tokens = typeName.split(' ')
            typeNameNew = tokens[0]
            unit = tokens[1]
            table.rename(columns={typeName: typeNameNew}, inplace=True)
            table[typeNameNew] = table[typeNameNew].astype(f"pint{unit}")

        # drop index levels representing case fields with precisely one option
        if not keepSingularIndexLevels:
            table.index = table.index.droplevel([level.name for level in table.index.levels if len(level)==1])

        return TEDataTable(self._tid, table)


    # insert missing periods
    def _insertMissingPeriods(self, table: pd.DataFrame) -> pd.DataFrame:
        # TODO: insert year of publication instead of current year
        table = table.fillna({'period': 2023})

        # return
        return table


    # apply mappings between entry types
    def _applyTypeMappings(self, table: pd.DataFrame) -> pd.DataFrame:
        # ---------- 1a. Convert fopex_rel entries to fopex ----------

        # copy fopex_rel entries to edit them safely
        selected_rows = table.query(f"type.isin({['fopex_rel']})").copy()

        # iterate over entries that need editing
        for index, row in selected_rows.iterrows():

            # ---- query for each row of fopex_rel the corresponding capex rows
            matching_entries = table.query(f"type.isin({['capex']})").copy()
            
            # check that other columns which might be Nan are equal
            for col in self._caseFields + ['component', 'src_ref']:
                if row[col] == row[col]:
                    matching_entries = matching_entries.query(f"{col}.isin({[row[col]]})")
            
            if(len(matching_entries) > 0):
                selected_rows.at[index,'value'] *= matching_entries['value'].iloc[0]

                selected_rows.at[index,'type'] = 'fopex'

                selected_rows.at[index,'unit'] = matching_entries['unit'].iloc[0] + '/a'
            else:
                # delete the corresponding row cause without fitting CAPEX entry from the same source, this value becomes meaningless
                table = table.drop([index])
                selected_rows = selected_rows.drop([index])
            
        # override main dataset
        table.loc[table['type'] == 'fopex_rel'] = selected_rows

        # ---------- 1b. Convert fopex to fopex_spec ----------
        convFacRep = convUnit(self.getRepUnit('fopex') + '*a', self.getRepUnit('fopex_spec'))
        convFacRef = convUnit(self.getRefUnit('fopex') + '*a', self.getRefUnit('fopex_spec'), self._tspecs['reference_flow'])

        rowsFOPEX = table['type'] == 'fopex'
        table.loc[rowsFOPEX, 'unit'] = table.loc[rowsFOPEX, 'unit'].apply(lambda u: str(ureg(u + '*a').to_reduced_units().u))
        table.loc[rowsFOPEX, 'value'] *= convFacRep / convFacRef
        table.loc[rowsFOPEX, 'type'] = 'fopex_spec'

        # ---------- 2. Convert full load hours entries to operational capacity factor ----------

        # copy fopex_rel entries to edit them safely
        selected_rows = table.query(f"type.isin({['flh']})").copy()

        # convert entries to ocf
        # value doesnt need to be changed because the standard time unit is year, so full load hours will already be converted to years by now
        selected_rows['type'] = 'ocf'
        selected_rows['unit'] = "dimensionless"

        # override main dataset
        table.loc[table['type'] == 'flh'] = selected_rows

        # ---------- 3. Convert energy_eff entries to energy_dem ----------

        if 'reference_flow' in techs[self._tid]:

            # copy energy_eff entries to edit them safely
            selected_rows = table.query(f"type.isin({['energy_eff']})").copy()
            reference_flow = techs[self._tid]['reference_flow']

            # iterate over entries that need editing
            for index, row in selected_rows.iterrows():
                # derive units based on reference_flow

                # has to be set to elec here because energy_eff entries dont have flow_type
                unit_from = flowTypes['elec']['default_unit']
                unit_to = flowTypes[reference_flow]['default_unit']
                conversionFactor = convUnit(unit_from=unit_from, unit_to=unit_to, flow_type=reference_flow)

                # convert entry to energy_dem
                selected_rows.at[index, 'value'] = conversionFactor * (1.0/row['value'])
                selected_rows.at[index, 'type'] = 'demand'
                selected_rows.at[index, 'unit'] = unit_from
                selected_rows.at[index, 'reference_unit'] = unit_to

            # override main dataset
            table.loc[table['type'] == 'energy_eff'] = selected_rows
        else:
            # no reference_flow found; energy_eff cannot be converted and has to be dropped
            entriesToDrop = table.query(f"type.isin({['energy_eff']})")
            table = table.drop(entriesToDrop.index.values)

        return table.reset_index(drop=True)


    # expand based on subtechs, modes, and period
    def _expandTechs(self, table: pd.DataFrame, expandCols: dict) -> pd.DataFrame:
        # loop over affected columns (subtech and mode)
        for colID, colVals in expandCols.items():
            table = pd.concat([
                table[table[colID].notna() & table[colID].isin(colVals)],
                table[table[colID].isna()].drop(columns=[colID]).merge(pd.DataFrame.from_dict({colID: colVals}), how='cross'),
            ]) \
            .reset_index(drop=True)

        # return
        return table


    # group by identifying columns and select periods/generate time series
    def _selectPeriods(self, table: pd.DataFrame, period: float | list | np.ndarray) -> pd.DataFrame:
        # list of columns to group by
        groupCols = ['type'] + self._caseFields + ['component', 'src_ref']

        # perform groupby and do not drop NA values
        grouped = table.groupby(groupCols, dropna=False)

        # create return list
        ret = []

        # loop over groups
        for keys, ids in grouped.groups.items():
            # get rows in group
            rows = table.loc[ids, ['period', 'value']]

            # get a list of periods that exist
            periodsExist = rows['period'].unique()

            # create dataframe containing rows for all requested periods
            reqRows = pd.DataFrame.from_dict({
                'period': period,
                'period_upper': [min([ip for ip in periodsExist if ip >= p], default=np.nan) for p in period],
                'period_lower': [max([ip for ip in periodsExist if ip <= p], default=np.nan) for p in period],
            })

            # set missing columns from group
            reqRows[groupCols] = keys

            # extrapolate
            condExtrapolate = (reqRows['period_upper'].isna() | reqRows['period_lower'].isna())
            rowsExtrapolate = reqRows.loc[condExtrapolate] \
                .assign(period_combined=lambda x: np.where(x.notna()['period_upper'], x['period_upper'], x['period_lower'])) \
                .merge(rows.rename(columns={'period': 'period_combined'}), on='period_combined')

            # interpolate
            rowsInterpolate = reqRows.loc[~condExtrapolate] \
                .merge(rows.rename(columns={c: f"{c}_upper" for c in rows.columns}), on='period_upper') \
                .merge(rows.rename(columns={c: f"{c}_lower" for c in rows.columns}), on='period_lower') \
                .assign(value=lambda row: row['value_lower'] + (row['period_upper'] - row['period']) /
                       (row['period_upper'] - row['period_lower']) * (row['value_upper'] - row['value_lower']))

            # combine into one dataframe and drop unused columns
            rowsAppend = pd.concat([rowsExtrapolate, rowsInterpolate]) \
                .drop(columns=['period_upper', 'period_lower', 'period_combined', 'value_upper', 'value_lower'])

            # add to return list
            ret.append(rowsAppend)

        # convert return list to dataframe and return
        return pd.concat(ret).reset_index(drop=True)


    # apply masks
    def _applyMasks(self, table, masks_other, masks_database) -> pd.DataFrame:
        # compile all masks into list
        masks = masks_other if masks_other is not None else []
        if masks_database and self._tid in defaultMasks:
            masks += defaultMasks[self._tid]

        # set weight from masks
        table['weight'] = 1.0
        for mask in masks:
            q = ' & '.join([f"{key}=='{val}'" for key, val in mask['query'].items()])
            table.loc[table.query(q).index, 'weight'] = mask['weight']

        # drop entries with zero weight and apply weights to values otherwise
        table = table.query('weight!=0.0').reset_index(drop=True)
        table['value'] *= table['weight']
        table.drop(columns=['weight'], inplace=True)

        return table