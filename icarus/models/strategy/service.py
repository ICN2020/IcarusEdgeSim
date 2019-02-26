# -*- coding: utf-8 -*-
"""Implementations of all service-based strategies"""
from __future__ import division
from __future__ import print_function

import networkx as nx
import random
import sys

from math import ceil
from icarus.registry import register_strategy
from icarus.util import inheritdoc, path_links
from .base import Strategy
from icarus.models.service import Task

__all__ = [
       'StrictestDeadlineFirst',
       'MostFrequentlyUsed',
       'Hybrid',
       'Lru',
       'Coordinated'
           ]

# Status codes
REQUEST = 0
RESPONSE = 1
TASK_COMPLETE = 2
# Admission results:
DEADLINE_MISSED = 0
CONGESTION = 1
SUCCESS = 2
CLOUD = 3
NO_INSTANCES = 4

@register_strategy('COORDINATED')
class Coordinated(Strategy):
    """
    A coordinated approach to route requests:
    i) Use global congestion information on Computation Spots to route requests.
    ii) Use global demand distribution to place services on Computation Spots.
    """
    def __init__(self, view, controller, replacement_interval=10, debug=False, p = 0.5, **kwargs):
        super(Coordinated, self).__init__(view,controller)
        self.last_replacement = 0
        self.replacement_interval = replacement_interval
        self.receivers = view.topology().receivers()
        self.topo = view.topology()
        self.compSpots = self.view.service_nodes()
        self.num_nodes = len(self.compSpots.keys())
        self.num_services = self.view.num_services()
        self.serviceNodeUtil = [None]*len(self.receivers)
        self.numVMsPerService = [[0] * self.num_services for x in range(self.num_nodes)]
        self.debug = debug
        self.p = p
        for node in self.compSpots.keys():
            cs = self.compSpots[node]
            if cs.is_cloud:
                continue
            #cs.numOfVMs = cs.service_population_size*cs.numOfCores
            #cs.scheduler.numOfVMs = cs.numOfVMs
            ### Place numOfCores instances for each service type 
            ### at each cs to avoid insufficient VMs. 
            ### The only constraint is the numOfCores. 
            for serv in range(self.num_services):
                cs.numberOfServiceInstances[serv] = 0
            for vm in range(cs.numOfVMs):
                serv = vm % cs.service_population_size 
                cs.numberOfServiceInstances[serv] += 1 
                self.numVMsPerService[cs.node][serv] = cs.numberOfServiceInstances[serv]

        for recv in self.receivers:
            recv = int(recv[4:])
            self.serviceNodeUtil[recv] = [None]*self.num_nodes
            for n in self.compSpots.keys():
                cs = self.compSpots[n]
                if cs.is_cloud:
                    continue
                self.serviceNodeUtil[recv][n] = [0.0]*self.num_services
            
    def initialise_metrics(self):
        """
        Initialise metrics/counters to 0
        """
        for node in self.compSpots.keys():
            cs = self.compSpots[node]
            cs.scheduler.idleTime = 0.0

        for recv in self.receivers:
            recv = int(recv[4:])
            self.serviceNodeUtil[recv] = [None]*self.num_nodes
            for n in self.compSpots.keys():
                cs = self.compSpots[n]
                if cs.is_cloud:
                    continue
                self.serviceNodeUtil[recv][n] = [0.0]*self.num_services

    def find_topmost_feasible_node(self, receiver, flow_id, path, time, service, deadline, rtt_delay):
        """
        finds fathest comp. spot to schedule a request using current 
        congestion information at each upstream comp. spot.
        The goal is to carry out computations at the farthest node to create space for
        tasks that require closer comp. spots.
        """

        source = self.view.content_source(service)
        # start from the upper-most node in the path and check feasibility
        upstream_node = source
        aTask = None
        for n in reversed(path[1:-1]):
            cs = self.compSpots[n]
            if cs.is_cloud:
                continue
            if cs.numberOfServiceInstances[service] == 0:
                continue
            delay = self.view.path_delay(receiver, n)
            rtt_to_cs = rtt_delay + 2*delay
            serviceTime = cs.services[service].service_time
            if deadline - time - rtt_to_cs < serviceTime:
                continue
            aTask = Task(time, deadline, rtt_to_cs, n, service, serviceTime, flow_id, receiver, time+delay)
            cs.scheduler.upcomingTaskQueue.append(aTask)
            cs.scheduler.upcomingTaskQueue = sorted(cs.scheduler.upcomingTaskQueue, key=lambda x: x.arrivalTime)
            cs.compute_completion_times(time, self.debug)
            for task in cs.scheduler._taskQueue + cs.scheduler.upcomingTaskQueue:
                if self.debug:
                    print("After compute_completion_times:")
                    task.print_task()
                if (task.expiry - delay) < task.completionTime:
                    cs.scheduler.upcomingTaskQueue.remove(aTask)
                    if self.debug:
                        print ("Task with flow_id " + str(task.flow_id) + " is violating its expiration time")
                    break
            else:
                source = n
                if self.debug:
                    print ("Completion time for flow_id: " + str(flow_id) + " at node: " + str(source) +  " is: " + str(aTask.completionTime))
                return source
        return source

    # Coordinated
    def replace_services(self, time):
        """
        Replace services in a coordinated manner. 
        Starting from the topmost node, deploy VMs for popular services, updating 
        the global demand as they are deployed. 
        """
        for height in range(self.topo.graph['height']+1):
            nodes = []
            ### Get nodes with depth = height #
            for node in self.compSpots.keys():
                cs = self.compSpots[node]
                if cs.is_cloud:
                    continue
                if self.topo.node[node]['depth'] == height:
                    nodes.append(node)
                    ### initialise number of service instances to zero #
                    for service in range(self.num_services):
                        cs.numberOfServiceInstances[service] = 0
                
            for node in nodes:
                cs = self.compSpots[node]
                service_utils = {x:0.0 for x in range(self.num_services)}
                ### sort services that are executable at the node by their utilisation #
                for recv in self.receivers:
                    ap = int(recv[4:])
                    for service in range(self.num_services):
                        if self.serviceNodeUtil[ap][node][service] == 0:
                            continue
                        service_obj = self.view.services()[service]
                        if service_obj.deadline > (2*self.view.path_delay(recv, node) + service_obj.service_time):
                            service_utils[service] += self.serviceNodeUtil[ap][node][service]
                service_utils_sorted = sorted(service_utils.items(), key = lambda x: x[1], reverse=True)
                
                if self.debug:
                    count = 0
                    for s,u in service_utils_sorted:
                        print (str(s) + " has utilisation " + str(u))
                        count += 1
                        if count == 10:
                            break
                
                remaining_vms = cs.numOfVMs
                for service, util in service_utils_sorted:
                    num_vms = int(round(util/(self.replacement_interval)))
                    if remaining_vms > 0 and num_vms > 0:
                        for recv in self.receivers:
                            ap = int(recv[4:])
                            if self.serviceNodeUtil[ap][node][service] == 0:
                                continue
                            else:
                                path = self.view.shortest_path(recv, node)
                                for n in path[1:]:
                                   self.serviceNodeUtil[ap][n][service] = 0.0
                    cs.numberOfServiceInstances[service] = min(num_vms, remaining_vms)
                    if self.debug and cs.numberOfServiceInstances[service] > 0:
                        print(str(cs.numberOfServiceInstances[service]) + " vms are instantiated at node: " + str(node) + " for service: " + str(service))
                    remaining_vms -= cs.numberOfServiceInstances[service]

                    if remaining_vms == 0:
                        break
                while remaining_vms > 0:
                    newAddition = False
                    for service, util in service_utils_sorted:
                        if(cs.numberOfServiceInstances[service] > 0):
                            newAddition = True
                            cs.numberOfServiceInstances[service] += 1
                            remaining_vms -= 1
                            if self.debug:
                                print ("One additional vm is instantiated at node: " + str(node) + " for service: " + str(service))
                        else:
                            num_vms = int(ceil(util/(self.replacement_interval)))
                            num_vms = min(num_vms, remaining_vms)
                            if num_vms > 0:
                                cs.numberOfServiceInstances[service] = num_vms
                                newAddition = True
                                remaining_vms -= num_vms
                                for recv in self.receivers:
                                    ap = int(recv[4:])
                                    if self.serviceNodeUtil[ap][node][service] == 0:
                                        continue
                                    else:
                                        path = self.view.shortest_path(recv, node)
                                        for n in path[1:]:
                                           self.serviceNodeUtil[ap][n][service] = 0.0
                                if self.debug:
                                    print (str(num_vms) + " additional vm is instantiated at node: " + str(node) + " for service: " + str(service))
                        if remaining_vms == 0:
                            break
                    if newAddition is False:
                        break
        # Report the VM instantiations (diff with the previous timeslot)
        for node in self.compSpots.keys():
            servicesToReplace = []
            servicesToAdd = []
            cs = self.compSpots[node]
            if cs.is_cloud:
                continue
            for service in range(self.num_services):
                diff = cs.numberOfServiceInstances[service] - self.numVMsPerService[cs.node][service]
                self.numVMsPerService[cs.node][service] = cs.numberOfServiceInstances[service]
                if diff > 0:
                    for indx in range(diff):
                        servicesToAdd.append(service)
                elif diff < 0:
                    for indx in range(diff):
                        servicesToReplace.append(service)
            for indx in range(len(servicesToAdd)):
                serviceToAdd = servicesToAdd[indx]
                serviceToReplace = servicesToReplace[indx] if indx < len(servicesToReplace) else None
                self.controller.reassign_vm(node, serviceToReplace, servicesToAdd)

    # Coordinated            
    @inheritdoc(Strategy)
    def process_event(self, time, receiver, content, log, node, flow_id, deadline, rtt_delay, status):
        """
        response : True, if this is a response from the cloudlet/cloud
        deadline : deadline for the request 
        flow_id : Id of the flow that the request/response is part of
        node : the current node at which the request/response arrived
        """
        service = content
        if self.debug:
            print ("\nEvent\n time: " + repr(time) + " receiver  " + repr(receiver) + " service " + repr(service) + " node " + repr(node) + " flow_id " + repr(flow_id) + " deadline " + repr(deadline) + " status " + repr(status)) 
        source = self.view.content_source(service)
        if time - self.last_replacement > self.replacement_interval:
            #self.print_stats()
            self.controller.replacement_interval_over(flow_id, self.replacement_interval, time)
            self.replace_services(time)
            self.last_replacement = time
            self.initialise_metrics()
        # Process request based on status
        if receiver == node and status == REQUEST:
            self.controller.start_session(time, receiver, service, log, flow_id, deadline)
            path = self.view.shortest_path(node, source)
            upstream_node = self.find_topmost_feasible_node(receiver, flow_id, path, time, service, deadline, rtt_delay)
            delay = self.view.path_delay(node, upstream_node)
            rtt_delay += delay*2
            if upstream_node != source:
                self.controller.add_event(time+delay, receiver, service, upstream_node, flow_id, deadline, rtt_delay, REQUEST)
                if self.debug:
                    print ("Request is scheduled to run at: " + str(upstream_node))
            else: #request is to be executed in the cloud and returned to receiver
                services = self.view.services() 
                serviceTime = services[service].service_time
                self.controller.add_event(time+rtt_delay+serviceTime, receiver, service, receiver, flow_id, deadline, rtt_delay, RESPONSE)
                if self.debug:
                    print ("Request is scheduled to run at the CLOUD")
            for n in path[1:]:
                cs = self.view.compSpot(n)
                if cs.is_cloud:
                    continue
                self.serviceNodeUtil[int(receiver[4:])][n][service] += self.view.services()[service].service_time
            return
        elif status == REQUEST and node != source:
            compSpot = self.view.compSpot(node)
            #check if the request is in the upcomingTaskQueue
            for thisTask in compSpot.scheduler.upcomingTaskQueue:
                if thisTask.flow_id == flow_id:
                    break
            else: # task for the flow_id is not in the Queue
                raise ValueError("No task with the given flow id in the upcomingTaskQueue")
            newTask = compSpot.scheduler.schedule(time)
            if newTask is not None:
                self.controller.add_event(newTask.completionTime, newTask.receiver, newTask.service, compSpot.node, newTask.flow_id, newTask.expiry, newTask.rtt_delay, TASK_COMPLETE) 
                self.controller.execute_service(newTask.flow_id, newTask.service, compSpot.node, time, compSpot.is_cloud)
                if self.debug:
                    print ("Task is scheduled to run: ")
                    newTask.print_task()
            else:
                if self.debug:
                    print ("Task is not ready to be executed: ")
                    compSpot.scheduler.print_core_status()
                    print (str(len(compSpot.scheduler._taskQueue)) + " tasks in the taskQueue")
                    for aTask in compSpot.scheduler._taskQueue:
                        aTask.print_task()
                    print (str(len(compSpot.scheduler.upcomingTaskQueue)) + " tasks in the upcomingtaskQueue")
                    for aTask in compSpot.scheduler.upcomingTaskQueue:
                        aTask.print_task()

        elif status == TASK_COMPLETE:
            compSpot = self.view.compSpot(node)
            newTask = compSpot.scheduler.schedule(time)
            if newTask is not None:
                self.controller.add_event(newTask.completionTime, newTask.receiver, newTask.service, compSpot.node, newTask.flow_id, newTask.expiry, newTask.rtt_delay, TASK_COMPLETE) 
                self.controller.execute_service(newTask.flow_id, newTask.service, compSpot.node, time, compSpot.is_cloud)
            
            delay = self.view.path_delay(node, receiver)
            self.controller.add_event(time+delay, receiver, service, receiver, flow_id, deadline, rtt_delay, RESPONSE)
            if self.debug:
                print("Flow id: " + str(flow_id) + " is scheduled to arrive at the receiver at time: " + str(time+delay))
            #if (time+delay > deadline): ### XXX After replacement, number of VMs change and requests can miss their deadline
            #    print ("Response at receiver at time: " + str(time+delay) + " deadline: " + str(deadline))
            #    raise ValueError ("A scheduled request has missed its deadline!")
        elif status == RESPONSE and node == receiver:
            self.controller.end_session(True, time, flow_id) #TODO add flow_time
            if self.debug:
                print ("Response reached the receiver at time: " + str(time) + " deadline: " + str(deadline))
            return
        else:
            print("This should not happen in Coordinated")
            sys.exit("Unexpected request in Coordinated strategy")

# LRU
@register_strategy('LRU')
class Lru(Strategy):
    """A distributed approach for service-centric routing
    """
    def __init__(self, view, controller, replacement_interval=10, debug=False, p = 0.5, **kwargs):
        super(Lru, self).__init__(view,controller)
        self.last_replacement = 0
        self.replacement_interval = replacement_interval
        self.receivers = view.topology().receivers()
        self.compSpots = self.view.service_nodes()
        self.num_nodes = len(self.compSpots.keys())
        self.num_services = self.view.num_services()
        self.debug = debug
        self.p = p

    def initialise_metrics(self):
        """
        Initialise metrics/counters to 0
        """
        for node in self.compSpots.keys():
            cs = self.compSpots[node]
            cs.running_requests = [0 for x in range(0, self.num_services)]
            cs.missed_requests = [0 for x in range(0, self.num_services)]
            cs.scheduler.idleTime = 0.0

    @inheritdoc(Strategy)
    def process_event(self, time, receiver, content, log, node, flow_id, deadline, rtt_delay, status):
        """
        response : True, if this is a response from the cloudlet/cloud
        deadline : deadline for the request 
        flow_id : Id of the flow that the request/response is part of
        node : the current node at which the request/response arrived
        """
        
        service = content
        source = self.view.content_source(service)

        if time - self.last_replacement > self.replacement_interval:
            #self.print_stats()
            self.controller.replacement_interval_over(flow_id, self.replacement_interval, time)
            self.last_replacement = time
            self.initialise_metrics()
        if receiver == node and status == REQUEST:
            self.controller.start_session(time, receiver, service, log, flow_id, deadline)
            path = self.view.shortest_path(node, source)
            next_node = path[1]
            delay = self.view.path_delay(node, next_node)
            rtt_delay += delay*2
            self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, REQUEST)
            return

        if self.debug:
            print ("\nEvent\n time: " + repr(time) + " receiver  " + repr(receiver) + " service " + repr(service) + " node " + repr(node) + " flow_id " + repr(flow_id) + " deadline " + repr(deadline) + " status " + repr(status)) 

        compSpot = None
        if self.view.has_computationalSpot(node):
            compSpot = self.view.compSpot(node)
        else: # the node has no computational spots (0 services)
            if status is not RESPONSE:
                if node == source:
                    print ("Error: reached the source node: " + repr(node) + " this should not happen!")
                    return
                path = self.view.shortest_path(node, source)
                next_node = path[1]
                delay = self.view.link_delay(node, next_node)
                if self.debug:
                    print ("Pass upstream (no compSpot) to node: " + repr(next_node) + " " + repr(time+delay))
                self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay+2*delay, REQUEST)
                return
        
        if status == RESPONSE: 
            # response is on its way back to the receiver
            if node == receiver:
                self.controller.end_session(True, time, flow_id) #TODO add flow_time
                return
            else:
                path = self.view.shortest_path(node, receiver)
                next_node = path[1]
                delay = self.view.link_delay(node, next_node)
                self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, RESPONSE)

        elif status == TASK_COMPLETE:
            task = compSpot.scheduler.schedule(time)
            #schedule the next queued task at this node
            if task is not None:
                self.controller.add_event(task.completionTime, task.receiver, task.service, node, task.flow_id, task.expiry, task.rtt_delay, TASK_COMPLETE)
                self.controller.execute_service(task.flow_id, task.service, compSpot.node, time, compSpot.is_cloud)

            # forward the completed task
            path = self.view.shortest_path(node, receiver)
            next_node = path[1]
            delay = self.view.link_delay(node, next_node)
            path_delay = self.view.path_delay(node, receiver)
            if (time + path_delay) > deadline and node != source:
                print("LRU missed a deadline for flow: " + str(flow_id))
            self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, RESPONSE)
            
        elif status == REQUEST:
            # Processing a request
            path = self.view.shortest_path(node, source)
            next_node = path[1]
            delay = self.view.path_delay(node, next_node)
            ret, reason = compSpot.admit_task(service, time, flow_id, deadline, receiver, rtt_delay, self.controller, self.debug)
            if ret == False:
                if reason == NO_INSTANCES:
                    # is upstream possible to execute
                    if deadline - time - rtt_delay - 2*delay < compSpot.services[service].service_time:
                        evicted = self.controller.put_content(node, service)
                        compSpot.reassign_vm(self.controller, self.controller, evicted, service, self.debug)
                    elif self.p == 1.0 or random.random() <= self.p:
                        evicted = self.controller.put_content(node, service)
                        compSpot.reassign_vm(self.controller, evicted, service, self.debug)
                rtt_delay += 2*delay
                self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, REQUEST)
            else:
                self.controller.get_content(node, service)

@register_strategy('HYBRID')
class Hybrid(Strategy):
    """A distributed approach for service-centric routing
    """
    
    def __init__(self, view, controller, replacement_interval=10, debug=False, n_replacements=1, **kwargs):
        super(Hybrid, self).__init__(view,controller)
        self.replacement_interval = replacement_interval
        self.n_replacements = n_replacements
        self.last_replacement = 0
        self.receivers = view.topology().receivers()
        self.compSpots = self.view.service_nodes()
        self.num_nodes = len(self.compSpots.keys())
        self.num_services = self.view.num_services()
        self.debug = debug
        # metric to rank each VM of Comp. Spot
        self.deadline_metric = {x : {} for x in range(0, self.num_nodes)}
        self.cand_deadline_metric = {x : {} for x in range(0, self.num_nodes)}
        self.replacements_so_far = 0
        for node in self.compSpots.keys():
            cs = self.compSpots[node]
            if cs.is_cloud:
                continue
            for vm_indx in range(0, self.num_services):
                self.deadline_metric[node][vm_indx] = 0.0
            for service_indx in range(0, self.num_services):
                self.cand_deadline_metric[node][service_indx] = 0.0
    
    # Hybrid
    def initialise_metrics(self):
        """
        Initialise metrics/counters to 0
        """
        for node in self.compSpots.keys():
            cs = self.compSpots[node]
            if cs.is_cloud:
                continue
            cs.running_requests = [0 for x in range(0, self.num_services)]
            cs.missed_requests = [0 for x in range(0, self.num_services)]
            cs.scheduler.idleTime = 0.0
            for vm_indx in range(0, self.num_services):
                self.deadline_metric[node][vm_indx] = 0.0
            for service_indx in range(0, self.num_services):
                self.cand_deadline_metric[node][service_indx] = 0.0

    #HYBRID 
    def replace_services1(self, time):
        for node, cs in self.compSpots.items():
            n_replacements = 0
            if cs.is_cloud:
                continue
            runningServiceResidualTimes = self.deadline_metric[node]
            missedServiceResidualTimes = self.cand_deadline_metric[node]
            #service_residuals = []
            running_services_utilisation_normalised = []
            missed_services_utilisation = []
            delay = {}
            #runningServicesUtil = {}
            #missedServicesUtil = {}

            util = []
            util_normalised = {}
            
            if self.debug:
                print ("Replacement at node " + repr(node))
            for service in range(0, self.num_services):
                d_metric = 0.0
                u_metric = 0.0
                util.append([service, (cs.missed_requests[service] + cs.running_requests[service]) * cs.services[service].service_time])
                if cs.numberOfServiceInstances[service] == 0: 
                # No instances
                    if cs.missed_requests[service] > 0:
                        d_metric = 1.0*missedServiceResidualTimes[service]/cs.missed_requests[service]
                    else:
                        d_metric = float('inf')
                    delay[service] = d_metric
                    u_metric = cs.missed_requests[service] * cs.services[service].service_time
                    if u_metric > self.replacement_interval:
                        u_metric = self.replacement_interval
                    #missedServiceResidualTimes[service] = d_metric
                    #missedServicesUtil[service] = u_metric
                    missed_services_utilisation.append([service, u_metric])
                    #service_residuals.append([service, d_metric])
                elif cs.numberOfServiceInstances[service] > 0: 
                # At least one instance
                    if cs.running_requests[service] > 0:
                        d_metric = 1.0*runningServiceResidualTimes[service]/cs.running_requests[service]
                    else:
                        d_metric = float('inf')
                    runningServiceResidualTimes[service] = d_metric
                    u_metric_missed = (cs.missed_requests[service]) * cs.services[service].service_time * 1.0
                    if u_metric_missed > self.replacement_interval:
                        u_metric_missed = self.replacement_interval
                    #missedServicesUtil[service] = u_metric_missed
                    u_metric_served = (1.0*cs.running_requests[service]*cs.services[service].service_time)/cs.numberOfServiceInstances[service]
                    #runningServicesUtil[service] = u_metric_served
                    missed_services_utilisation.append([service, u_metric_missed])
                    #running_services_latency.append([service, d_metric])
                    running_services_utilisation_normalised.append([service, u_metric_served/cs.numberOfServiceInstances[service]])
                    #service_residuals.append([service, d_metric])
                    delay[service] = d_metric
                else:
                    print("This should not happen")
            running_services_utilisation_normalised = sorted(running_services_utilisation_normalised, key=lambda x: x[1]) #smaller to larger
            missed_services_utilisation = sorted(missed_services_utilisation, key=lambda x: x[1], reverse=True) #larger to smaller
            #service_residuals = sorted(service_residuals, key=lambda x: x[1]) #smaller to larger
            exit_loop = False
            for service_missed, missed_util in missed_services_utilisation:
                if exit_loop:
                    break
                for indx in range(len(running_services_utilisation_normalised)):
                    service_running = running_services_utilisation_normalised[indx][0]
                    running_util = running_services_utilisation_normalised[indx][1]
                    if running_util > missed_util:
                        exit_loop = True
                        break
                    if service_running == service_missed:
                        continue
                    if missed_util >= running_util and delay[service_missed] < delay[service_running] and delay[service_missed] > 0:
                        cs.reassign_vm(self.controller, service_running, service_missed, self.debug)
                        if self.debug:
                            print ("Missed util: " + str(missed_util) + " running util: " + str(running_util) + " Adequate time missed: " + str(delay[service_missed]) + " Adequate time running: " + str(delay[service_running]))
                        del running_services_utilisation_normalised[indx]
                        n_replacements += 1
                        break
            if self.debug:
                print(str(n_replacements) + " replacements at node:" + str(cs.node) + " at time: " + str(time))
                for node in self.compSpots.keys():
                    cs = self.compSpots[node]
                    if cs.is_cloud:
                        continue
                    if cs.node != 14 and cs.node!=6:
                        continue
                    for service in range(0, self.num_services):
                        if cs.numberOfServiceInstances[service] > 0:
                            print ("Node: " + str(node) + " has " + str(cs.numberOfServiceInstances[service]) + " instance of " + str(service))

    #HYBRID 
    @inheritdoc(Strategy)
    def process_event(self, time, receiver, content, log, node, flow_id, deadline, rtt_delay, status):
        """
        response : True, if this is a response from the cloudlet/cloud
        deadline : deadline for the request 
        flow_id : Id of the flow that the request/response is part of
        node : the current node at which the request/response arrived
        """
        #self.debug = False
        #if node == 12:
        #    self.debug = True

        service = content
        source = self.view.content_source(service)

        if time - self.last_replacement > self.replacement_interval:
            #self.print_stats()
            print("Replacement time: " + repr(time))
            self.controller.replacement_interval_over(flow_id, self.replacement_interval, time)
            self.replace_services1(time)
            self.last_replacement = time
            self.initialise_metrics()
        
        compSpot = None
        if self.view.has_computationalSpot(node):
            compSpot = self.view.compSpot(node)

        # Request reached the cloud
        if source == node and status == REQUEST:
            ret, reason = compSpot.admit_task(service, time, flow_id, deadline, receiver, rtt_delay, self.controller, self.debug)
            if ret == False:
                print("This should not happen in Hybrid.")
                raise ValueError("Task should not be rejected at the cloud.")
            return 

        # Request at the receiver
        if receiver == node and status == REQUEST:
            self.controller.start_session(time, receiver, service, log, flow_id, deadline)
            path = self.view.shortest_path(node, source)
            next_node = path[1]
            delay = self.view.path_delay(node, next_node)
            rtt_delay += delay*2
            self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, REQUEST)
            return

        if self.debug:
            print ("\nEvent\n time: " + repr(time) + " receiver  " + repr(receiver) + " service " + repr(service) + " node " + repr(node) + " flow_id " + repr(flow_id) + " deadline " + repr(deadline) + " status " + repr(status)) 

        if status == RESPONSE: 
            # response is on its way back to the receiver
            if node == receiver:
                self.controller.end_session(True, time, flow_id) #TODO add flow_time
                return
            else:
                path = self.view.shortest_path(node, receiver)
                next_node = path[1]
                delay = self.view.link_delay(node, next_node)
                path_del = self.view.path_delay(node, receiver)
                self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, RESPONSE)
                if path_del + time > deadline:
                    compSpot.missed_requests[service] += 1 

        elif status == TASK_COMPLETE:
            if node != source:
                task = compSpot.scheduler.schedule(time)
                #schedule the next queued task at this node
                if task is not None:
                    self.controller.add_event(task.completionTime, task.receiver, task.service, node, task.flow_id, task.expiry, task.rtt_delay, TASK_COMPLETE)
                    self.controller.execute_service(task.flow_id, task.service, compSpot.node, time, compSpot.is_cloud)

                    if task.expiry < time + self.view.path_delay(node, task.receiver):
                        print("Error in Hybrid: Time: " + repr(time) + " Node: " + repr(node) + " Deadline: " + repr(task.expiry) + " task rtt: " + repr(task.rtt_delay) + " Exec time: " + repr(task.exec_time) + " Flow ID: " + repr(task.flow_id))
                        #raise ValueError("This should not happen: a task missed its deadline after being executed at an edge node.")

            # forward the completed task
            path = self.view.shortest_path(node, receiver)
            next_node = path[1]
            delay = self.view.link_delay(node, next_node)
            self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, RESPONSE)
            
        elif status == REQUEST:
            # Processing a request
            source = self.view.content_source(service)
            path = self.view.shortest_path(node, source)
            next_node = path[1]
            delay = self.view.path_delay(node, next_node)
            deadline_metric = (deadline - time - rtt_delay - compSpot.services[service].service_time) #/deadline
            if self.debug:
                print ("Deadline metric: " + repr(deadline_metric))
            if self.view.has_service(node, service):
                if self.debug:
                    print ("Calling admit_task")
                ret, reason = compSpot.admit_task(service, time, flow_id, deadline, receiver, rtt_delay, self.controller, self.debug)
                if self.debug:
                    print ("Done Calling admit_task")
                if ret is False:    
                    # Pass the Request upstream
                    rtt_delay += delay*2
                    self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, REQUEST)
                    if deadline_metric > 0:
                        self.cand_deadline_metric[node][service] += deadline_metric
                    if self.debug:
                        print ("Pass upstream to node: " + repr(next_node))
                    #compSpot.missed_requests[service] += 1 # Added here
                else:
                    if deadline_metric > 0:
                        self.deadline_metric[node][service] += deadline_metric
            else: #Not running the service
                #compSpot.missed_requests[service] += 1
                if self.debug:
                    print ("Not running the service: Pass upstream to node: " + repr(next_node))
                rtt_delay += delay*2
                self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, REQUEST)
                if deadline_metric > 0:
                    self.cand_deadline_metric[node][service] += deadline_metric
        else:
            print ("Error: unrecognised status value : " + repr(status))


# Highest Utilisation First Strategy 
@register_strategy('MFU')
class MostFrequentlyUsed(Strategy):
    """A distributed approach for service-centric routing
    """
    
    def __init__(self, view, controller, replacement_interval=10, debug=False, n_replacements=1, **kwargs):
        super(MostFrequentlyUsed, self).__init__(view,controller)
        self.replacement_interval = replacement_interval
        self.n_replacements = n_replacements
        self.last_replacement = 0
        self.receivers = view.topology().receivers()
        self.compSpots = self.view.service_nodes()
        self.num_nodes = len(self.compSpots.keys())
        self.num_services = self.view.num_services()
        self.debug = debug
        # metric to rank each VM of Comp. Spot
        self.deadline_metric = {x : {} for x in range(0, self.num_nodes)}
        self.cand_deadline_metric = {x : {} for x in range(0, self.num_nodes)}
        for node in self.compSpots.keys():
            cs = self.compSpots[node]
            if cs.is_cloud:
                continue
            for vm_indx in range(0, self.num_services):
                self.deadline_metric[node][vm_indx] = 0.0
            for service_indx in range(0, self.num_services):
                self.cand_deadline_metric[node][service_indx] = 0.0
    
    # MFU
    def initialise_metrics(self):
        """
        Initialise metrics/counters to 0
        """
        for node in self.compSpots.keys():
            cs = self.compSpots[node]
            if cs.is_cloud:
                continue
            cs.running_requests = [0 for x in range(0, self.num_services)]
            cs.missed_requests = [0 for x in range(0, self.num_services)]
            cs.scheduler.idleTime = 0.0
            for vm_indx in range(0, self.num_services):
                self.deadline_metric[node][vm_indx] = 0.0
            for service_indx in range(0, self.num_services):
                self.cand_deadline_metric[node][service_indx] = 0.0
    # MFU
    def replace_services(self, k, time):
        """
        This method does the following:
        1. Evaluate instantiated and stored services at each computational spot for the past time interval, ie, [t-interval, t]. 
        2. Decide which services to instantiate in the next time interval [t, t+interval].
        Parameters:
        k : max number of instances to replace at each computational spot
        interval: the length of interval
        """

        for node, cs in self.compSpots.items():
            if cs.is_cloud:
                continue
            n_replacements = k
            vms = []
            cand_services = []
            vm_metrics = self.deadline_metric[node]
            cand_metric = self.cand_deadline_metric[node]
            if self.debug:
                print ("Replacement at node " + repr(node))
            for service in range(0, self.num_services):
                usage_metric = 0.0
                if cs.numberOfServiceInstances[service] == 0:
                    if self.debug:
                        print ("No running instances of service: " + repr(service)) 
                    continue
                if cs.running_requests[service] == 0:
                    usage_metric = 0.0 
                    if self.debug:
                        print ("No scheduled requests for service: " + repr(service)) 
                else:
                    usage_metric = cs.running_requests[service] * cs.services[service].service_time
                    if self.debug:
                        print ("Usage metric for service: " + repr(service) + " is " + repr(usage_metric))

                vms.append([service, usage_metric])
            
            for service in range(0, self.num_services):
                #if cs.numberOfServiceInstances[service] > 0:
                #    continue
                if cs.missed_requests[service] == 0:
                    usage_metric = 0.0
                else:
                    usage_metric = cs.missed_requests[service] * cs.services[service].service_time
                    if self.debug:
                        print ("Usage metric for Upstream Service: " + repr(service) + " is " + repr(usage_metric))
                    cand_services.append([service, usage_metric])

            # sort vms and virtual_vms arrays according to metric
            vms = sorted(vms, key=lambda x: x[1]) #small to large
            cand_services = sorted(cand_services, key=lambda x: x[1], reverse=True) #large to small
            if self.debug:
                print ("VMs: " + repr(vms))
                print ("Cand. Services: " + repr(cand_services))
            # Small metric is better
            indx = 0
            for vm in vms: 
                if indx >= len(cand_services):
                    break
                if vm[1] > cand_services[indx][1]:
                    break
                else:
                    # Are they the same service? This should not happen really
                    if vm[0] != cand_services[indx][0]:
                        cs.reassign_vm(self.controller, vm[0], cand_services[indx][0], self.debug)
                        n_replacements -= 1
                
                if n_replacements == 0 or indx == len(cand_services):
                    break
                indx += 1

    # MFU
    @inheritdoc(Strategy)
    def process_event(self, time, receiver, content, log, node, flow_id, deadline, rtt_delay, status):
        """
        response : True, if this is a response from the cloudlet/cloud
        deadline : deadline for the request 
        flow_id : Id of the flow that the request/response is part of
        node : the current node at which the request/response arrived
        """
        service = content
        source = self.view.content_source(service)

        if time - self.last_replacement > self.replacement_interval:
            #self.print_stats()
            self.controller.replacement_interval_over(flow_id, self.replacement_interval, time)
            self.replace_services(self.n_replacements, time)
            self.last_replacement = time
            self.initialise_metrics()
        
        compSpot = None
        if self.view.has_computationalSpot(node):
            compSpot = self.view.compSpot(node)
        
        # Request reached the cloud
        if source == node and status == REQUEST:
            ret, reason = compSpot.admit_task(service, time, flow_id, deadline, receiver, rtt_delay, self.controller, self.debug)
            if ret == False:
                print("This should not happen in Hybrid.")
                raise ValueError("Task should not be rejected at the cloud.")
            return 

        # Request at the receiver
        if receiver == node and status == REQUEST:
            self.controller.start_session(time, receiver, service, log, flow_id, deadline)
            source = self.view.content_source(service)
            path = self.view.shortest_path(node, source)
            next_node = path[1]
            delay = self.view.path_delay(node, next_node)
            rtt_delay += delay*2
            self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, REQUEST)
            return

        if self.debug:
            print ("\nEvent\n time: " + repr(time) + " receiver  " + repr(receiver) + " service " + repr(service) + " node " + repr(node) + " flow_id " + repr(flow_id) + " deadline " + repr(deadline) + " status " + repr(status)) 

        # MFU
        if status == RESPONSE: 
            # response is on its way back to the receiver
            if node == receiver:
                self.controller.end_session(True, time, flow_id) #TODO add flow_time
                return
            else:
                path = self.view.shortest_path(node, receiver)
                next_node = path[1]
                delay = self.view.link_delay(node, next_node)
                self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, RESPONSE)

        elif status == TASK_COMPLETE:
            if node != source:
                task = compSpot.scheduler.schedule(time)
                #schedule the next queued task at this node
                if task is not None:
                    self.controller.add_event(task.completionTime, task.receiver, task.service, node, task.flow_id, task.expiry, task.rtt_delay, TASK_COMPLETE)
                    self.controller.execute_service(task.flow_id, task.service, compSpot.node, time, compSpot.is_cloud)

                    if task.expiry < time + self.view.path_delay(node, task.receiver):
                        print("Error. Time: " + repr(time) + " Node: " + repr(node) + " Deadline: " + repr(task.expiry) + " task rtt: " + repr(task.rtt_delay) + " Exec time: " + repr(task.exec_time) + " Flow ID: " + repr(task.flow_id))
                        raise ValueError("This should not happen: a task missed its deadline after being executed at an edge node (non-cloud).")

            # forward the completed task
            path = self.view.shortest_path(node, receiver)
            next_node = path[1]
            delay = self.view.link_delay(node, next_node)
            self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, RESPONSE)
            
        # MFU
        elif status == REQUEST:
            # Processing a request
            source = self.view.content_source(service)
            path = self.view.shortest_path(node, source)
            next_node = path[1]
            delay = self.view.path_delay(node, next_node)
            deadline_metric = (deadline - time - rtt_delay - compSpot.services[service].service_time)/deadline
            if self.debug:
                print ("Deadline metric: " + repr(deadline_metric))
            if self.view.has_service(node, service):
                if self.debug:
                    print ("Calling admit_task")
                ret, reason = compSpot.admit_task(service, time, flow_id, deadline, receiver, rtt_delay, self.controller, self.debug)
                if self.debug:
                    print ("Done Calling admit_task")
                if ret is False:    
                    # Pass the Request upstream
                    rtt_delay += delay*2
                    self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, REQUEST)
                    if deadline_metric > 0:
                        self.cand_deadline_metric[node][service] += deadline_metric
                    if self.debug:
                        print ("Pass upstream to node: " + repr(next_node))
                else:
                    if deadline_metric > 0:
                        self.deadline_metric[node][service] += deadline_metric
            else: #Not running the service
                compSpot.missed_requests[service] += 1
                if self.debug:
                    print ("Not running the service: Pass upstream to node: " + repr(next_node))
                rtt_delay += delay*2
                self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, REQUEST)
                if deadline_metric > 0:
                    self.cand_deadline_metric[node][service] += deadline_metric
        else:
            print ("Error: unrecognised status value : " + repr(status))

# Strictest Deadline First Strategy
@register_strategy('SDF')
class StrictestDeadlineFirst(Strategy):
    """ A distributed approach for service-centric routing
    """
    # SDF  
    def __init__(self, view, controller, replacement_interval=10, debug=False, n_replacements=1, **kwargs):
        super(StrictestDeadlineFirst, self).__init__(view,controller)
        self.replacement_interval = replacement_interval
        self.n_replacements = n_replacements
        self.last_replacement = 0
        self.receivers = view.topology().receivers()
        self.compSpots = self.view.service_nodes()
        self.num_nodes = len(self.compSpots.keys())
        self.num_services = self.view.num_services()
        self.debug = debug
        # metric to rank each VM of Comp. Spot
        self.deadline_metric = {x : {} for x in range(0, self.num_nodes)}
        self.cand_deadline_metric = {x : {} for x in range(0, self.num_nodes)}
        for node in self.compSpots.keys():
            cs = self.compSpots[node]
            if cs.is_cloud:
                continue
            for vm_indx in range(0, self.num_services):
                self.deadline_metric[node][vm_indx] = 0.0
            for service_indx in range(0, self.num_services):
                self.cand_deadline_metric[node][service_indx] = 0.0
    
    # SDF  
    def initialise_metrics(self):
        """
        Initialise metrics/counters to 0
        """
        for node in self.compSpots.keys():
            cs = self.compSpots[node]
            if cs.is_cloud:
                continue
            cs.running_requests = [0 for x in range(0, self.num_services)]
            cs.missed_requests = [0 for x in range(0, self.num_services)]
            cs.scheduler.idleTime = 0.0
            for vm_indx in range(0, self.num_services):
                self.deadline_metric[node][vm_indx] = 0.0
            for service_indx in range(0, self.num_services):
                self.cand_deadline_metric[node][service_indx] = 0.0
    # SDF  
    def replace_services(self, k, time):
        """
        This method does the following:
        1. Evaluate instantiated and stored services at each computational spot for the past time interval, ie, [t-interval, t]. 
        2. Decide which services to instantiate in the next time interval [t, t+interval].
        Parameters:
        k : max number of instances to replace at each computational spot
        interval: the length of interval
        """

        for node, cs in self.compSpots.items():
            if cs.is_cloud:
                continue
            n_replacements = k
            vms = []
            cand_services = []
            vm_metrics = self.deadline_metric[node]
            cand_metric = self.cand_deadline_metric[node]
            if self.debug:
                print ("Replacement at node " + repr(node))
            for service in range(0, self.num_services):
                if cs.numberOfServiceInstances[service] == 0:
                    if self.debug:
                        print ("No running instances of service: " + repr(service)) 
                    continue
                d_metric = 0.0
                if cs.running_requests[service] == 0:
                    d_metric = 1.0 
                    if self.debug:
                        print ("No scheduled requests for service: " + repr(service)) 
                else:
                    d_metric = vm_metrics[service]/cs.running_requests[service]
                    if self.debug:
                        print ("Deadline metric for service: " + repr(service) + " is " + repr(d_metric))

                vms.append([service, d_metric])
            # SDF  
            for service in range(0, self.num_services):
                #if self.debug:
                #    print ("\tNumber of Requests for upstream service " + repr(service) + " is "  + repr(cs.missed_requests[service]))
                d_metric = 0.0
                if cs.missed_requests[service] == 0:
                    d_metric = 1.0
                else:
                    d_metric = cand_metric[service]/cs.missed_requests[service]
                    if self.debug:
                        print ("Deadline metric for Upstream Service: " + repr(service) + " is " + repr(d_metric))
                    cand_services.append([service, d_metric])
            # sort vms and virtual_vms arrays according to metric
            vms = sorted(vms, key=lambda x: x[1], reverse=True) #larger to smaller
            cand_services = sorted(cand_services, key=lambda x: x[1]) #smaller to larger
            if self.debug:
                print ("VMs: " + repr(vms))
                print ("Cand. Services: " + repr(cand_services))
            # Small metric is better
            indx = 0
            for vm in vms:
                # vm's metric is worse than the cand. and they are not the same service
                if indx >= len(cand_services):
                    break
                if vm[1] < cand_services[indx][1]:
                    break
                else:
                    if vm[0] != cand_services[indx][0]:
                       cs.reassign_vm(self.controller, vm[0], cand_services[indx][0], self.debug)
                       n_replacements -= 1
                
                if n_replacements == 0 or indx == len(cand_services):
                    break
                indx += 1
    # SDF  
    @inheritdoc(Strategy)
    def process_event(self, time, receiver, content, log, node, flow_id, deadline, rtt_delay, status):
        """
        response : True, if this is a response from the cloudlet/cloud
        deadline : deadline for the request 
        flow_id : Id of the flow that the request/response is part of
        node : the current node at which the request/response arrived
        """
        service = content
        source = self.view.content_source(service)

        if time - self.last_replacement > self.replacement_interval:
            #self.print_stats()
            self.controller.replacement_interval_over(flow_id, self.replacement_interval, time)
            self.replace_services(self.n_replacements, time)
            self.last_replacement = time
            self.initialise_metrics()
        
        compSpot = None
        if self.view.has_computationalSpot(node):
            compSpot = self.view.compSpot(node)

        # Request reached the cloud
        if source == node and status == REQUEST:
            ret, reason = compSpot.admit_task(service, time, flow_id, deadline, receiver, rtt_delay, self.controller, self.debug)
            if ret == False:
                print("This should not happen in Hybrid.")
                raise ValueError("Task should not be rejected at the cloud.")
            return 

        # Request at the receiver
        if receiver == node and status == REQUEST:
            self.controller.start_session(time, receiver, service, log, flow_id, deadline)
            source = self.view.content_source(service)
            path = self.view.shortest_path(node, source)
            next_node = path[1]
            delay = self.view.path_delay(node, next_node)
            rtt_delay += delay*2
            self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, REQUEST)
            return

        if self.debug:
            print ("\nEvent\n time: " + repr(time) + " receiver  " + repr(receiver) + " service " + repr(service) + " node " + repr(node) + " flow_id " + repr(flow_id) + " deadline " + repr(deadline) + " status " + repr(status)) 

        # SDF  
        if status == RESPONSE: 
            # response is on its way back to the receiver
            if node == receiver:
                self.controller.end_session(True, time, flow_id) #TODO add flow_time
                return
            else:
                path = self.view.shortest_path(node, receiver)
                next_node = path[1]
                delay = self.view.link_delay(node, next_node)
                self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, RESPONSE)

        elif status == TASK_COMPLETE:
            if node != source:
                task = compSpot.scheduler.schedule(time)
                #schedule the next queued task at this node
                if task is not None:
                    self.controller.add_event(task.completionTime, task.receiver, task.service, node, task.flow_id, task.expiry, task.rtt_delay, TASK_COMPLETE)
                    self.controller.execute_service(task.flow_id, task.service, compSpot.node, time, compSpot.is_cloud)

                    if task.expiry < time + self.view.path_delay(node, task.receiver):
                        print("Error. Time: " + repr(time) + " Node: " + repr(node) + " Deadline: " + repr(task.expiry) + " task rtt: " + repr(task.rtt_delay) + " Exec time: " + repr(task.exec_time) + " Flow ID: " + repr(task.flow_id))
                        raise ValueError("This should not happen: a task missed its deadline after being executed at an edge node (non-cloud).")

            # forward the completed task
            path = self.view.shortest_path(node, receiver)
            next_node = path[1]
            delay = self.view.link_delay(node, next_node)
            self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, RESPONSE)
            
        # SDF  
        elif status == REQUEST:
            # Processing a request
            source = self.view.content_source(service)
            path = self.view.shortest_path(node, source)
            next_node = path[1]
            delay = self.view.path_delay(node, next_node)
            deadline_metric = (deadline - time - rtt_delay - compSpot.services[service].service_time)/deadline
            if self.debug:
                print ("Deadline metric: " + repr(deadline_metric))
            if self.view.has_service(node, service):
                if self.debug:
                    print ("Calling admit_task")
                ret, reason = compSpot.admit_task(service, time, flow_id, deadline, receiver, rtt_delay, self.controller, self.debug)
                if self.debug:
                    print ("Done Calling admit_task")
                if ret is False:    
                    # Pass the Request upstream
                    rtt_delay += delay*2
                    self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, REQUEST)
                    if deadline_metric > 0:
                        self.cand_deadline_metric[node][service] += deadline_metric
                    if self.debug:
                        print ("Pass upstream to node: " + repr(next_node))
                else:
                    if deadline_metric > 0:
                        self.deadline_metric[node][service] += deadline_metric
            else: #Not running the service
                compSpot.missed_requests[service] += 1
                if self.debug:
                    print ("Not running the service: Pass upstream to node: " + repr(next_node))
                rtt_delay += delay*2
                self.controller.add_event(time+delay, receiver, service, next_node, flow_id, deadline, rtt_delay, REQUEST)
                if deadline_metric > 0:
                    self.cand_deadline_metric[node][service] += deadline_metric
        else:
            print ("Error: unrecognised status value : " + repr(status))

