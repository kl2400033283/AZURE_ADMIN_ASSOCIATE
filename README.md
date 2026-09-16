Requirements : -    
    1.An active Azure subscription with permissions to create Load Balancers, Virtual  
    2. Machines/VMSS, Virtual networks and Network Security Groups
    3.Standard SKU Load Balancer (Basic SKU does not support outbound rules)
    4.A backend pool of VMs or a VM Scale Set that can run a simple load-generation script
    5.Azure Monitor enabled with diagnostic settings on the Load Balancer to capture SNAT metrics (Used SNAT Ports, Allocated SNAT Ports, SNAT Connection Count)
    6.A shared external endpoint to send outbound test traffic to (e.g., a public HTTP echo/test service)
    7.Ability to scale the backend pool up/down during the exercise (manually or via auto scale rules)
