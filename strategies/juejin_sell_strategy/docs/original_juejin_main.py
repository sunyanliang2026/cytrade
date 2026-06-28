# coding=utf-8
from __future__ import print_function, absolute_import, unicode_literals
import numpy as np
import pandas as pd
import logging
from gm.api import *
from datetime import datetime, time
'''
1. 强势股低开，严重不及预期，开盘卖
2. 高点超过3.5%，先保证别把盈利做成大亏，回落3个点止损。
3. 高点超过5%, 或封涨停后，跌破4%止损
3. 高点超过8.5%,未曾封涨停, 跌破7%止赢
4. 涨停 - 前10分钟，非一字开，封板1分钟内开板，还在争夺，补量，大概率马上回封，先不卖 20251028 达华智能卖飞，其他情况开板先卖一笔落袋(板砸，封单小于1E，当前tick成交>5kw)
        - 5分钟不回封，卖出
5. 跌破-1%，开盘3分钟后，反弹到2%，在跌破2%，卖出
6. 开盘高于-5.5%，跌破-6%，挂反弹卖出，认赔
7. 开盘高于-9%，跌停，清仓，认赔
8. 开盘低于-9%，人工参与
9. 名牌辨识度龙头不按以上规则（合富中国，平潭发展，分歧最低点卖飞）

后续- 非竞价备选买标的，冲高就卖了
'''


def init(context):
    # 进行基础配置，设置日志级别和格式
    logging.basicConfig(
        level=logging.DEBUG, # 设置日志级别为DEBUG，会记录DEBUG及以上级别的日志
        format='%(asctime)s - %(message)s', # 定义日志输出格式
        datefmt='%Y-%m-%d', # 定义时间格式
        handlers=[
            logging.FileHandler("app.log"), # 将日志写入app.log文件
            logging.StreamHandler()         # 同时将日志输出到控制台
        ]
    )

    # 获取一个记录器（Logger），通常以当前模块名__name__命名
    logger = logging.getLogger(__name__)
    
    context.symbol = []
    col_default = []
    positions = context.account().positions()
    for position in positions:
        print(position)
    init_data = pd.read_csv('sell_10.csv')
    for c in init_data['symbol']:
        context.symbol.append(c)
        col_default.append(0)

    init_data.index = init_data['symbol']
    init_data['flag'] = col_default
    #init_data['exp'] = col_default
    init_data['pre_bid'] = col_default
    init_data['updown'] = col_default
    init_data['open_flag'] = col_default
    init_data['timestamp_1'] = col_default
    init_data['timestamp_2'] = col_default
    init_data['timestamp_10'] = col_default
    init_data['up_pr'] = col_default
    init_data['lo_pr'] = col_default
    init_data['up_sell'] = col_default
    init_data['pre_close'] = col_default
    init_data['pre_flag'] = col_default
    init_data['pre_up_amt'] = col_default
    context.mydata = init_data
    logger.info(context.mydata)

    subscribe(symbols=context.symbol, frequency='tick')

def on_tick(context, tick):
    tsymb = tick['symbol']
    topen = tick['open']
    thigh = tick['high']
    tlow = tick['low']
    logger = logging.getLogger(__name__)
    
    if context.now.time() < time(9,15) :
        return
    
    date = context.now.strftime('%Y-%m-%d %H:%M:%S')
    quotes = tick['quotes'][0]
    # 获取买一价
    bid_p = quotes['bid_p']
    bid_v = quotes['bid_v']
    # 获取卖一价
    ask_p = quotes['ask_p']
    ask_v = quotes['ask_v']
    last_amount = tick['last_amount']
    if context.mydata.at[tsymb,'up_pr'] == 0 or context.mydata.at[tsymb,'pre_close'] == 0 :
        context.mydata.at[tsymb,'up_pr'] = get_symbols(1010, symbols=tsymb)[0]['upper_limit']
        context.mydata.at[tsymb,'lo_pr'] = get_symbols(1010, symbols=tsymb)[0]['lower_limit']
        context.mydata.at[tsymb,'pre_close'] = get_symbols(1010, symbols=tsymb)[0]['pre_close']
    up_pr = context.mydata.at[tsymb,'up_pr']
    lo_pr = context.mydata.at[tsymb,'lo_pr']
    pre_close = context.mydata.at[tsymb,'pre_close']

    position_long = context.account().position(symbol=tsymb, side=PositionSide_Long)
    #if position_long == None:
    #    return
 
    if date[11:13] == '09' and int(date[14:16]) == 24 and int(date[17:19]) >=56:
        #if topen >= pre_close*0.97 and topen < pre_close-0.01:
        if bid_p > pre_close*0.94 and bid_p < pre_close*0.995 and context.mydata.at[tsymb,'exp'] == 1:
            if position_long != None and context.mydata.at[tsymb,'flag'] == 0:
                sellvol = int(context.mydata.at[tsymb,'sellvol'])
                vol2 = position_long.available_now - sellvol
                order_volume(tsymb, sellvol, OrderSide_Sell, order_type=OrderType_Limit, position_effect=PositionEffect_Close, price=lo_pr)
                order_volume(tsymb, vol2, OrderSide_Sell, order_type=OrderType_Limit, position_effect=PositionEffect_Close, price=round(pre_close*1.05,2))
                #set flag
                context.mydata.at[tsymb,'flag'] = 1
                #context.mydata.at[tsymb,'exp'] == -1
                logger.info('{}:{}, status:{}'.format(
                    context.now.strftime('%H:%M:%S'),tsymb,context.mydata.at[tsymb,'flag']))
                logger.info('竞价卖出 ----------------------------- bid_p ---------------------------------------------------------------------------------------严重不及预期------- 1')
    
    if context.now.time() < time(9,26) or context.now.time() > time(14,57):
        return
    #20251201 四川金顶冲高没卖，为啥3分钟后？
    #if context.mydata.at[tsymb,'exp'] == 1 and context.now.time() < time(9,33):
    #    return
    #------------------------------------------------------------------------------------------

    if position_long != None and context.mydata.at[tsymb,'exp'] == 1 and bid_p > pre_close*0.94 and bid_p < pre_close*0.98 and context.now.time() < time(9,31):
        CancalOrder(tsymb)
        CancalOrder(tsymb)
        sellvol = int(context.mydata.at[tsymb,'sellvol'])
        if position_long.available_now < sellvol:
            sellvol = position_long.available_now
        order_volume(tsymb, sellvol, OrderSide_Sell, order_type=OrderType_Limit,position_effect=PositionEffect_Close, price=round(bid_p * 0.99,2))
        context.mydata.at[tsymb,'exp'] == -1
        logger.info('{}:{}, status:{}'.format(
            context.now.strftime('%H:%M:%S'),tsymb,context.mydata.at[tsymb,'flag']))
        logger.info('卖出 ----------------------------- bid_p ---------------------------------------------------------------------------------------严重不及预期------- 1')
        
    #记录首次真涨停时间，10状态需要跌破7才变成5，也就是跌破7才能再从非10到10
    if bid_p==up_pr and context.mydata.at[tsymb,'flag'] != 10:
        context.mydata.at[tsymb,'timestamp_2'] = context.now
    #真涨停，封单大于1.2E，否则还是走7的状态，回落就卖了，这里不能非10才进这个逻辑，不破7都是10，10状态下open_flag状态需要改变
    if bid_p==up_pr and bid_v*bid_p > 120000000 and context.mydata.at[tsymb,'flag'] != 99:
        context.mydata.at[tsymb,'flag'] = 10
        #context.mydata.at[tsymb,'timestamp_2'] = context.now
        context.mydata.at[tsymb,'open_flag'] = 0
    if bid_p < pre_close*0.975 and context.mydata.at[tsymb,'flag'] != 99 and context.mydata.at[tsymb,'flag'] != -3 and context.mydata.at[tsymb,'flag'] != 66:
        context.mydata.at[tsymb,'flag'] = -3

    if context.mydata.at[tsymb,'flag'] == -3 and context.mydata.at[tsymb,'pre_bid'] >= pre_close*1.02 and bid_p < pre_close*1.02 and tlow < pre_close*0.97 and context.now.time() > time(9,33):
        if position_long != None:
            CancalOrder(tsymb)
            sellvol = int(context.mydata.at[tsymb,'sellvol'])
            if position_long.available_now < sellvol:
                sellvol = position_long.available_now
            order_volume(tsymb, sellvol, OrderSide_Sell, order_type=OrderType_Limit,position_effect=PositionEffect_Close, price=round(bid_p * 0.99,2))
            vol2 = position_long.available_now - sellvol
            order_volume(tsymb, vol2, OrderSide_Sell, order_type=OrderType_Limit, position_effect=PositionEffect_Close, price=round(pre_close*1.04,2))
            #set flag
            context.mydata.at[tsymb,'flag'] = 0
            logger.info('{}:{}, status:{}, {}'.format(
                context.now.strftime('%H:%M:%S'),context.mydata.at[tsymb,'nick'],context.mydata.at[tsymb,'flag'],'-------------933-------------- -3 反弹 and >2% -> 0 --试错离场---------------------'))
                
    #非跌停开，跌停止损清仓。向市场认错，认赔 --20251101
    if ask_p == lo_pr and topen>pre_close*0.91 and context.mydata.at[tsymb,'flag'] != 99:
        if position_long != None and ask_p == lo_pr and ask_v*ask_p > 50000000:
            CancalOrder(tsymb)
            CancalOrder(tsymb)
            if position_long.available_now > 0:
                sellvol = position_long.available_now
                ret = order_volume(tsymb, sellvol, OrderSide_Sell, order_type=OrderType_Limit, 
                    position_effect=PositionEffect_Close, price=lo_pr)
                #set flag
                context.mydata.at[tsymb,'flag'] = 99
                logger.info('{}:{}, status:{}, clear:{}'.format(
                    context.now.strftime('%H:%M:%S'),context.mydata.at[tsymb,'nick'],context.mydata.at[tsymb,'flag'],'----------------------------大低开，跌停止损 -> 清仓 --------------------------'))
                
    #高点超过4%, 或封涨停后，跌破3%止损 and thigh>=pre_close*1.065，不要时间限制，防止如来神掌-20251114
    #if context.mydata.at[tsymb,'flag'] == 5 and bid_p < pre_close*1.04 and context.now.time() > time(9,32):
    if context.mydata.at[tsymb,'flag'] == 5 and bid_p < pre_close*1.05:
        if context.now.time() < time(9,31) and topen<pre_close*1.05:
            #20251118 海南海药卖飞
            logger.info('{}:{}, status:{} {}'.format(
                context.now.strftime('%H:%M:%S'),context.mydata.at[tsymb,'nick'],context.mydata.at[tsymb,'flag'],' -----------------------第一分钟，开盘向上，跌破5暂时不卖 -----------------------------'))
        elif position_long != None and thigh>pre_close*1.065:
            CancalOrder(tsymb)
            sellvol = int(context.mydata.at[tsymb,'sellvol'])
            if position_long.available_now < sellvol:
                sellvol = position_long.available_now
            ret = order_volume(tsymb, sellvol, OrderSide_Sell, order_type=OrderType_Limit, position_effect=PositionEffect_Close, price=round(bid_p * 0.99,2))
            #set flag
            context.mydata.at[tsymb,'flag'] = -9
            logger.info('{}:{}, status:{}, {}'.format(
                context.now.strftime('%H:%M:%S'),context.mydata.at[tsymb,'nick'],context.mydata.at[tsymb,'flag'],'----------------------跌破5%，flag=-9, 不再走5状态，可以走-3，10的状态 flag 5 and <5% ---------------------------------'))
        elif position_long != None and bid_p < pre_close*1.03:
            CancalOrder(tsymb)
            sellvol = int(context.mydata.at[tsymb,'sellvol'])
            if position_long.available_now < sellvol:
                sellvol = position_long.available_now
            ret = order_volume(tsymb, sellvol, OrderSide_Sell, order_type=OrderType_Limit, position_effect=PositionEffect_Close, price=round(bid_p * 0.99,2))
            #set flag
            context.mydata.at[tsymb,'flag'] = -9
            logger.info('{}:{}, status:{}, {}'.format(
                context.now.strftime('%H:%M:%S'),context.mydata.at[tsymb,'nick'],context.mydata.at[tsymb,'flag'],'----------------------跌破5%，flag=-9, 不再走5状态，可以走-3，10的状态 flag 5 and <4% ---------------------------------'))
    #10不会变成7，只摸了涨停不是真涨停没变10，破7卖出，20251029 青岛双星封了涨停量没到1.2E，没到10，应该7卖，所以删掉thigh<up_pr
    #if thigh>pre_close*1.085 and thigh<up_pr and bid_p < pre_close*1.07 and context.mydata.at[tsymb,'flag'] == 7:
    if thigh>pre_close*1.085 and bid_p < pre_close*1.07 and context.mydata.at[tsymb,'flag'] == 7:
        if position_long != None:
            CancalOrder(tsymb)
            sellvol = int(context.mydata.at[tsymb,'sellvol'])
            if position_long.available_now < sellvol:
                sellvol = position_long.available_now
            ret = order_volume(tsymb, sellvol, OrderSide_Sell, order_type=OrderType_Limit, 
                position_effect=PositionEffect_Close, price=round(bid_p * 0.985,2)+0.01)
            #set flag
            context.mydata.at[tsymb,'flag'] = -9
            logger.info('{}:{}, status:{} {}'.format(
                context.now.strftime('%H:%M:%S'),context.mydata.at[tsymb,'nick'],context.mydata.at[tsymb,'flag'],' -----------------------到8.5没涨停，跌破7 and <7% -----------------------------'))
    #7个点够了，止盈买别的
    if bid_p < pre_close*1.065 and context.mydata.at[tsymb,'flag'] == 7 and context.now.time() < time(9,31):
        if position_long != None:
            CancalOrder(tsymb)
            sellvol = int(context.mydata.at[tsymb,'sellvol'])
            if position_long.available_now < sellvol:
                sellvol = position_long.available_now
            ret = order_volume(tsymb, sellvol, OrderSide_Sell, order_type=OrderType_Limit, 
                position_effect=PositionEffect_Close, price=round(bid_p * 0.99,2))
            #set flag
            context.mydata.at[tsymb,'flag'] = -9
            logger.info('{}:{}, status:{} {}'.format(
                context.now.strftime('%H:%M:%S'),context.mydata.at[tsymb,'nick'],context.mydata.at[tsymb,'flag'],' -----------------------到7没涨停，跌破7 and <7% -----------------------------'))

    #涨停后，开板5min不回封卖出 - 5分钟，最不济调到状态5
    if context.mydata.at[tsymb,'flag'] == 10 and context.mydata.at[tsymb,'open_flag'] != 10:
        #开板先卖一笔，落袋再说
        gap = context.now - context.mydata.at[tsymb,'timestamp_2']
        if bid_p < up_pr or (bid_p==up_pr and bid_v*bid_p < 100000000 and context.mydata.at[tsymb,'pre_up_amt']-bid_v*bid_p>25000000) or (bid_p==up_pr and bid_v*bid_p < 160000000 and context.mydata.at[tsymb,'pre_up_amt']-bid_v*bid_p>80000000):
            #前10分钟，非一字开，封板1分钟内开板，还在争夺，补量，大概率马上回封，先不卖 20251028 达华智能卖飞
            if gap.total_seconds() < 60 and topen < up_pr and context.now.time() < time(9,40):
                logger.info('{}, 前10分钟，非一字开，封板1分钟内开板，还在争夺，补量，大概率马上回封'.format(context.mydata.at[tsymb,'nick']))
            elif context.mydata.at[tsymb,'up_sell'] != 99:
                sellvol = int(context.mydata.at[tsymb,'sellvol'])
                if position_long != None and position_long.available_now < sellvol:
                    sellvol = position_long.available_now
                ret = order_volume(tsymb, sellvol, OrderSide_Sell, order_type=OrderType_Limit, 
                    position_effect=PositionEffect_Close, price=round(bid_p * 0.982,2))
                context.mydata.at[tsymb,'up_sell'] = 99
                
                logger.info('{}:{}, status:{}, {}'.format(
                    context.now.strftime('%H:%M:%S'),context.mydata.at[tsymb,'nick'],context.mydata.at[tsymb,'flag'],'--------------------------- 开板先卖一笔，落袋再说---------------------'))    
        if bid_p < up_pr:
            context.mydata.at[tsymb,'timestamp_10'] = context.now
            context.mydata.at[tsymb,'open_flag'] = 10
            if context.mydata.at[tsymb,'up_sell'] == 99:
                context.mydata.at[tsymb,'flag'] = -9
            
    
    if context.mydata.at[tsymb,'open_flag'] == 10 and bid_p < up_pr:
        #if bid_p < up_pr or (bid_p==up_pr and bid_v*bid_p < 10000000):
        gap = context.now - context.mydata.at[tsymb,'timestamp_10']
        if gap.total_seconds() > 300:
            if position_long != None:
                sellvol = int(context.mydata.at[tsymb,'sellvol'])
                if position_long.available_now < sellvol:
                    sellvol = position_long.available_now
                ret = order_volume(tsymb, sellvol, OrderSide_Sell, order_type=OrderType_Limit, 
                    position_effect=PositionEffect_Close, price=round(bid_p,2))
                #order_target_percent(symbol=tsymb, percent=0, position_side=PositionSide_Long, 
                #    order_type=OrderType_Limit, price=round(bid_p * 0.98,2)+0.01)
                #set buy flag
                context.mydata.at[tsymb,'flag'] = 0
                context.mydata.at[tsymb,'open_flag'] = 0
                context.mydata.at[tsymb,'timestamp_10'] = context.now
                logger.info('{}:{}, status:{}, gap:{}, bid:{} {}'.format(
                    context.now.strftime('%H:%M:%S'),context.mydata.at[tsymb,'nick'],context.mydata.at[tsymb,'flag'],gap.total_seconds(), bid_p,'---------------涨停后，开板5min不回封卖出-----------------'))

    #跌破7%，flag=-9, 不再走5、7状态，可以走-3, 10的状态 - 落袋一笔，剩余10开板、-3水下翻红，或人工尾盘处理 - 20250924
    if bid_p > pre_close*1.07 and bid_p < up_pr and context.mydata.at[tsymb,'flag'] >= 0 and context.mydata.at[tsymb,'flag'] != 10:
        context.mydata.at[tsymb,'flag'] = 7
    #跌破5%，flag=-9, 不再走5、7状态，可以走-3，10的状态
    if bid_p > pre_close*1.04 and bid_p < pre_close*1.07 and context.mydata.at[tsymb,'flag'] >= -3:
        context.mydata.at[tsymb,'flag'] = 5

    #记录买一，下个tick比较
    context.mydata.at[tsymb,'pre_bid'] = bid_p
    if bid_p == up_pr:
        context.mydata.at[tsymb,'pre_up_amt'] = bid_v*bid_p
    if date[15:16] == '0' and int(date[17:19]) < 3:
        logger.info('{}:标的:{} {}, bid_p{}, up_pr:{}, status:{}'.format(
            context.now.strftime('%H:%M:%S'),tsymb,context.mydata.at[tsymb,'nick'],bid_p,up_pr,context.mydata.at[tsymb,'flag']))
    if context.mydata.at[tsymb,'flag'] != context.mydata.at[tsymb,'pre_flag']:
        logger.info('{}:标的:{} {}, status:  {} -> {}'.format(
            context.now.strftime('%H:%M:%S'),tsymb,context.mydata.at[tsymb,'nick'],context.mydata.at[tsymb,'pre_flag'],context.mydata.at[tsymb,'flag']))
        context.mydata.at[tsymb,'pre_flag'] = context.mydata.at[tsymb,'flag']


def CancalOrder(symbol):
    unfin_order = get_unfinished_orders()
    for order in unfin_order:
        if order['symbol'] == symbol:
            order_cancel(wait_cancel_orders=[{'cl_ord_id': order['cl_ord_id'], 'account_id': order['account_id']}])

if __name__ == '__main__':
    '''
        strategy_id策略ID, 由系统生成
        filename文件名, 请与本文件名保持一致
        mode运行模式, 实时模式:MODE_LIVE回测模式:MODE_BACKTEST
        token绑定计算机的ID, 可在系统设置-密钥管理中生成
        backtest_start_time回测开始时间
        backtest_end_time回测结束时间
        backtest_adjust股票复权方式, 不复权:ADJUST_NONE前复权:ADJUST_PREV后复权:ADJUST_POST
        backtest_initial_cash回测初始资金
        backtest_commission_ratio回测佣金比例
        backtest_slippage_ratio回测滑点比例
        backtest_match_mode市价撮合模式，以下一tick/bar开盘价撮合:0，以当前tick/bar收盘价撮合：1
        '''
    run(strategy_id='61ec06b8-0100-11f1-98e1-e4b97a6af28c',
        filename='main.py',
        mode=MODE_BACKTEST,
        token='6307ad40aa060ac597ae33023ff8d89bc6df0e8d',
        backtest_start_time='2020-11-01 08:00:00',
        backtest_end_time='2020-11-10 16:00:00',
        backtest_adjust=ADJUST_PREV,
        backtest_initial_cash=10000000,
        backtest_commission_ratio=0.0001,
        backtest_slippage_ratio=0.0001,
        backtest_match_mode=1)

